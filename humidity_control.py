# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "adafruit-circuitpython-hdc302x",
#     "adafruit-blinka",
#     "python-kasa",
#     "sqlite-utils",
#     "httpx",
#     "RPi.GPIO",
#     "tzdata",
# ]
# ///

import asyncio
import logging
import math
import sys
from datetime import datetime

# Hardware & System Dependencies
import board
import adafruit_hdc302x
from kasa import Discover, Device
import sqlite_utils
import httpx

# ==============================================================================
# Configuration Parameters
# ==============================================================================

# Target Alias for the Kasa Smart Plug (Must match the exact alias on the network)
HUMIDIFIER_ALIAS = "Humidifier"

# NWS API Configuration
# The closest primary observation nexus is KJFK (Kennedy International).
NWS_URL = "https://api.weather.gov/stations/KJFK/observations/latest"
USER_AGENT = "BrooklynHumidityController/1.0 (admin@local.network)"

# Database Configuration
DB_FILE = "environmental_history.db"

# Polling Interval (in seconds)
POLL_INTERVAL = 60

# ==============================================================================
# Initialization & Logging
# ==============================================================================

# Configure idiomatic logging targeting stdout for optimal systemd/journald integration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("HumidityController")

# ==============================================================================
# Mathematical and Telemetry Helper Functions
# ==============================================================================

def calculate_max_humidity(outdoor_temp_f: float) -> float:
    """
    Calculates the maximum allowable indoor humidity using a smooth sigmoid function
    derived from empirical building science data for modern double-pane windows.
    Absolute cap is 45%, lower asymptote is 30%.
    """
    try:
        exponent = -0.09 * outdoor_temp_f
        if exponent > 50:
            return 30.0

        max_rh = 30.0 + (15.0 / (1.0 + math.exp(exponent)))
        return min(45.0, max(30.0, max_rh))
    except Exception as e:
        logger.warning(f"Unexpected error in humidity calculation: {e}. Using fallback 35.0%.")
        return 35.0 # Conservative safe fallback for double-pane windows


async def get_outdoor_temperature(client: httpx.AsyncClient) -> float:
    """
    Asynchronously fetches the latest outdoor temperature from the NWS API.
    Returns the temperature in Fahrenheit. Returns None on network/parsing failure.
    """
    headers = {"User-Agent": USER_AGENT}
    try:
        # Use a short timeout to prevent the event loop from hanging
        response = await client.get(NWS_URL, headers=headers, timeout=10.0)
        response.raise_for_status()
        data = response.json()

        # NWS data is deeply nested; safely extract using dict.get()
        temp_c = data.get("properties", {}).get("temperature", {}).get("value")
        if temp_c is None:
            logger.warning("NWS API returned null temperature data.")
            return None

        temp_f = (temp_c * 9/5) + 32
        return temp_f
    except httpx.RequestError as e:
        logger.warning(f"Network error communicating with NWS API: {e}")
        return None
    except Exception as e:
        logger.warning(f"Unexpected error parsing NWS API payload: {e}")
        return None

async def discover_humidifier(db: sqlite_utils.Database) -> Device:
    """
    Attempts to connect to the Kasa plug via a cached IP.
    Falls back to a UDP broadcast if the IP is stale or missing.
    """
    # Ensure a configuration table exists for key-value caching
    db["config"].create({"key": str, "value": str}, pk="key", if_not_exists=True)

    # 1. Attempt connection via cached IP
    cached_records = list(db["config"].rows_where("key = ?", ["humidifier_ip"]))
    if cached_records:
        cached_ip = cached_records[0]["value"]
        logger.info(f"Attempting direct connection to cached IP: {cached_ip}")
        try:
            dev = await Device.connect(host=cached_ip)
            if dev.alias == HUMIDIFIER_ALIAS:
                logger.info(f"Successfully reconnected to '{HUMIDIFIER_ALIAS}' at cached IP.")
                return dev
            else:
                logger.warning(f"Device at {cached_ip} is not '{HUMIDIFIER_ALIAS}'. Cache invalid.")
        except Exception as e:
            logger.warning(f"Direct connection to cached IP failed: {e}")
            await dev.disconnect()

    # 2. Fallback to UDP Broadcast Discovery
    logger.info("Initiating local UDP broadcast discovery for Kasa devices...")
    try:
        devices = await Discover.discover()

        for ip, dev in devices.items():
            await dev.update()
            if dev.alias == HUMIDIFIER_ALIAS:
                logger.info(f"Target smart plug '{HUMIDIFIER_ALIAS}' successfully resolved at {ip}")
                # Cache the newly discovered IP
                db["config"].upsert({"key": "humidifier_ip", "value": ip}, pk="key")
                return dev

        logger.critical(f"Fatal: Kasa device with alias '{HUMIDIFIER_ALIAS}' not found on the subnet.")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Fatal: Exception encountered during UDP device discovery: {e}")
        sys.exit(1)

# ==============================================================================
# Main Asynchronous Event Loop
# ==============================================================================

async def main():
    logger.info("Initializing Edge Environmental Controller...")

    # 1. Initialize the SQLite database
    try:
        db = sqlite_utils.Database(DB_FILE)
        logger.info(f"Successfully bound to SQLite telemetry database at {DB_FILE}")
    except Exception as e:
        logger.critical(f"Fatal: Failed to initialize SQLite database: {e}")
        sys.exit(1)

    # 2. Initialize the I2C Bus and HDC3022 Sensor via Adafruit Blinka
    try:
        i2c_bus = board.I2C()
        sensor = adafruit_hdc302x.HDC302x(i2c_bus)
        logger.info("Adafruit HDC3022 I2C environmental sensor initialized.")
    except ValueError as e:
        logger.critical(f"Fatal: I2C hardware bus or HDC3022 sensor not detected: {e}")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Fatal: Unexpected exception binding to HDC3022: {e}")
        sys.exit(1)

    # 3. Discover the Kasa Plug
    humidifier_plug = await discover_humidifier(db)

    # HTTPX Async Client for NWS API (persisted across iterations for TCP connection pooling)
    async with httpx.AsyncClient() as http_client:

        # Initialize State Variables
        last_known_outdoor_temp = 20.0  # Conservative safe-mode starting temperature

        logger.info("Initialization complete. Entering primary telemetry loop.")

        while True:
            try:
                # --------------------------------------------------------------
                # Phase A: Data Ingestion
                # --------------------------------------------------------------

                # Fetch Outdoor Data
                fetched_temp = await get_outdoor_temperature(http_client)
                if fetched_temp is not None:
                    last_known_outdoor_temp = fetched_temp

                # Fetch Indoor Telemetry
                indoor_temp_c = sensor.temperature
                indoor_temp_f = (indoor_temp_c * 9/5) + 32
                indoor_rh = sensor.relative_humidity

                # --------------------------------------------------------------
                # Phase B: Setpoint Calculation
                # --------------------------------------------------------------

                target_max_rh = calculate_max_humidity(last_known_outdoor_temp)
                upper_bound = target_max_rh - 1.0 # Turn OFF threshold
                lower_bound = target_max_rh - 5.0 # Turn ON threshold

                # --------------------------------------------------------------
                # Phase C: Actuation and Hysteresis Logic
                # --------------------------------------------------------------

                # We must update the plug to get its true current state.
                # If this fails (e.g., plug unplugged from wall), it will raise an exception.
                await humidifier_plug.update()
                humidifier_is_on = humidifier_plug.is_on
                action_taken = "NONE"

                if humidifier_is_on:
                    if indoor_rh >= upper_bound:
                        await humidifier_plug.turn_off()
                        humidifier_is_on = False
                        action_taken = "DEACTIVATED"
                else:
                    if indoor_rh <= lower_bound:
                        await humidifier_plug.turn_on()
                        humidifier_is_on = True
                        action_taken = "ACTIVATED"

                # --------------------------------------------------------------
                # Phase D: Logging and Persistence
                # --------------------------------------------------------------

                log_message = (
                    f"Out_T: {last_known_outdoor_temp:0.1f}°F | "
                    f"In_T: {indoor_temp_f:0.1f}°F | In_RH: {indoor_rh:0.1f}% | "
                    f"Max_RH: {target_max_rh:0.1f}% (Bounds: {lower_bound:0.1f}-{upper_bound:0.1f}) | "
                    f"State: {'ON' if humidifier_is_on else 'OFF'} | Action: {action_taken}"
                )
                logger.info(log_message)

                # Persist Telemetry to SQLite
                db["telemetry_log"].insert({
                    "timestamp": datetime.now().isoformat(),
                    "outdoor_temp_f": round(last_known_outdoor_temp, 2),
                    "indoor_temp_f": round(indoor_temp_f, 2),
                    "indoor_rh": round(indoor_rh, 2),
                    "target_max_rh": round(target_max_rh, 2),
                    "humidifier_state": humidifier_is_on,
                    "action_taken": action_taken
                })

            except Exception as e:
                # Catch failures (like network loss to the Kasa plug) and trigger fail-fast
                logger.critical(f"Fatal error encountered during telemetry loop iteration: {e}")
                logger.critical("Exiting application to allow systemd to re-initialize dependencies.")
                sys.exit(1)

            # --------------------------------------------------------------
            # Phase E: Loop Constraint
            # --------------------------------------------------------------
            await asyncio.sleep(POLL_INTERVAL)
        await humidifier_plug.disconnect()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Process interrupted by user. Shutting down cleanly.")
        sys.exit(0)