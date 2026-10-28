# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "adafruit-blinka",
#     "adafruit-circuitpython-hdc302x",
#     "aiohttp",
#     "gpiod>=2.2.2,<3",  # GPIO library that Blinka needs on the Pi
#     "python-kasa",
# ]
# ///
"""Humidistat: keeps room humidity below a limit that depends on outdoor temperature.

Every minute it reads an HDC302x sensor over I2C, checks the outdoor temperature
from the National Weather Service, switches a Kasa smart plug, and logs a row to
SQLite. It also serves a small status page on the local network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import signal
import sqlite3
import sys
import time
import tomllib
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import aiohttp
from aiohttp import web
from kasa import Device, Discover

log = logging.getLogger("humidistat")


# ==============================================================================
# Config
# ==============================================================================


@dataclass
class Config:
    plug_alias: str = "Humidifier"  # name of the Kasa plug, as set in the Kasa app
    nws_station: str = "KJFK"  # NWS observation station for outdoor temperature
    nws_contact: str = "https://github.com/SheinH/humidistat"  # NWS asks for contact info
    db_path: str = "humidistat.db"  # relative paths are relative to the config file
    poll_seconds: int = 60
    weather_refresh_seconds: int = 600  # NWS only updates about once an hour
    weather_max_age_hours: float = 3.0  # older than this, use the fallback temperature
    fallback_outdoor_temp_f: float = 0.0  # cold on purpose, so the limit errs low
    window_k: float = 0.5  # how cold the window edge gets: 0 = room temp, 1 = outdoor temp
    max_humidity_cap: float = 45.0  # never go above this, for comfort
    on_margin: float = 5.0  # turn on when humidity is this far below the max
    off_margin: float = 1.0  # turn off when humidity is this close to the max
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8080


def load_config(path: Path) -> Config:
    if not path.exists():
        log.info("No config file at %s, using defaults", path)
        return Config()
    data = tomllib.loads(path.read_text())
    unknown = set(data) - {f.name for f in fields(Config)}
    if unknown:
        sys.exit(f"Unknown setting(s) in {path}: {', '.join(sorted(unknown))}")
    return Config(**data)


# ==============================================================================
# Control logic
# ==============================================================================


def saturation_pressure(temp_c: float) -> float:
    """Water vapor pressure (hPa) at saturation; over ice below freezing (Magnus formula)."""
    if temp_c >= 0:
        return 6.112 * math.exp(17.62 * temp_c / (243.12 + temp_c))
    return 6.112 * math.exp(22.46 * temp_c / (272.62 + temp_c))


def humidity_limit(indoor_f: float, outdoor_f: float, cfg: Config) -> tuple[float, float]:
    """Max indoor humidity (%) before the window's coldest part fogs, and that glass temperature (°F).

    The bottom edges of a window get colder than the middle. We estimate their
    temperature as a point between room and outdoor temperature, then find the
    humidity at which the room's dew point would reach it.
    """
    glass_f = indoor_f - cfg.window_k * (indoor_f - outdoor_f)
    to_c = lambda f: (f - 32) * 5 / 9  # noqa: E731
    rh = 100 * saturation_pressure(to_c(glass_f)) / saturation_pressure(to_c(indoor_f))
    return min(cfg.max_humidity_cap, rh), glass_f


def should_be_on(is_on: bool, rh: float, max_rh: float, cfg: Config) -> bool:
    """On/off with a gap between thresholds so the plug doesn't flip constantly."""
    if is_on:
        return rh < max_rh - cfg.off_margin
    return rh <= max_rh - cfg.on_margin


# ==============================================================================
# Database
# ==============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry_log (
    timestamp TEXT,
    outdoor_temp_f FLOAT,
    indoor_temp_f FLOAT,
    indoor_rh FLOAT,
    target_max_rh FLOAT,
    humidifier_state INTEGER,
    action_taken TEXT
);
CREATE INDEX IF NOT EXISTS telemetry_log_timestamp ON telemetry_log (timestamp);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?)", (key, value))

    def log(self, row: dict) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO telemetry_log VALUES (:timestamp, :outdoor_temp_f, "
                ":indoor_temp_f, :indoor_rh, :target_max_rh, :humidifier_state, :action_taken)",
                row,
            )

    def history(self, since: datetime) -> list[tuple]:
        return self.db.execute(
            "SELECT timestamp, indoor_rh, indoor_temp_f, outdoor_temp_f, target_max_rh, "
            "humidifier_state FROM telemetry_log WHERE timestamp >= ? ORDER BY timestamp",
            (since.astimezone(timezone.utc).isoformat(),),
        ).fetchall()


# ==============================================================================
# Kasa plug
# ==============================================================================


class Plug:
    """Finds the plug by name and reconnects whenever it drops off."""

    def __init__(self, alias: str, store: Store):
        self.alias = alias
        self.store = store
        self.dev: Device | None = None

    async def connect(self) -> bool:
        if self.dev is not None:
            return True

        # Fast path: the IP it had last time.
        ip = self.store.get("plug_ip")
        if ip:
            try:
                dev = await Device.connect(host=ip)
                if dev.alias == self.alias:
                    self.dev = dev
                else:
                    log.info("Device at saved IP %s is %r, not %r", ip, dev.alias, self.alias)
                    await quiet_disconnect(dev)
            except Exception as e:
                log.info("Plug not reachable at saved IP %s: %s", ip, e)

        # Slow path: broadcast and look for it by name.
        if self.dev is None:
            log.info("Searching the network for plug %r", self.alias)
            try:
                found = await Discover.discover()
            except Exception as e:
                log.warning("Network search failed: %s", e)
                return False
            for dev in found.values():
                if self.dev is None and dev.alias == self.alias:
                    self.dev = dev
                else:
                    await quiet_disconnect(dev)
            if self.dev is None:
                log.warning("Plug %r not found, will search again next round", self.alias)
                return False

        self.store.put("plug_ip", self.dev.host)
        log.info("Connected to plug %r at %s", self.alias, self.dev.host)
        return True

    async def is_on(self) -> bool | None:
        """Current state, or None if the plug can't be reached."""
        if not await self.connect():
            return None
        try:
            await self.dev.update()
            return self.dev.is_on
        except Exception as e:
            log.warning("Lost contact with plug: %s", e)
            await self.drop()
            return None

    async def switch(self, on: bool) -> bool:
        try:
            await (self.dev.turn_on() if on else self.dev.turn_off())
            return True
        except Exception as e:
            log.warning("Couldn't switch plug %s: %s", "on" if on else "off", e)
            await self.drop()
            return False

    async def drop(self) -> None:
        if self.dev is not None:
            await quiet_disconnect(self.dev)
            self.dev = None


async def quiet_disconnect(dev: Device) -> None:
    try:
        await dev.disconnect()
    except Exception:
        pass


# ==============================================================================
# Outdoor weather
# ==============================================================================


async def fetch_outdoor_temp(http: aiohttp.ClientSession, cfg: Config) -> tuple[float, datetime] | None:
    """Latest temperature (°F) and observation time from the NWS, or None."""
    url = f"https://api.weather.gov/stations/{cfg.nws_station}/observations/latest"
    headers = {"User-Agent": f"humidistat ({cfg.nws_contact})", "Accept": "application/geo+json"}
    try:
        async with http.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
            r.raise_for_status()
            props = (await r.json(content_type=None))["properties"]
        temp_c = props["temperature"]["value"]
        if temp_c is None:
            log.warning("NWS returned no temperature for %s", cfg.nws_station)
            return None
        return temp_c * 9 / 5 + 32, datetime.fromisoformat(props["timestamp"])
    except Exception as e:
        log.warning("Weather fetch failed: %s", e)
        return None


# ==============================================================================
# Main loop
# ==============================================================================


@dataclass
class Status:
    """Latest reading, shown on the dashboard."""

    time: datetime | None = None
    indoor_rh: float | None = None
    indoor_temp_f: float | None = None
    outdoor_temp_f: float | None = None
    outdoor_observed_at: datetime | None = None
    outdoor_is_fallback: bool = False
    max_rh: float | None = None
    glass_temp_f: float | None = None  # estimated temperature of the window's coldest edge
    humidifier_on: bool | None = None  # None = plug not reachable


def open_sensor():
    import adafruit_hdc302x  # imported here: these only load on a Pi
    import board

    return adafruit_hdc302x.HDC302x(board.I2C())


async def run(cfg: Config, sensor, plug: Plug, store: Store, status: Status, stop: asyncio.Event):
    outdoor: tuple[float, datetime] | None = None
    last_weather_fetch = -math.inf

    async with aiohttp.ClientSession() as http:
        while not stop.is_set():
            started = time.monotonic()
            now = datetime.now(timezone.utc)

            # Outdoor temperature (fall back to a conservative value if stale).
            if started - last_weather_fetch >= cfg.weather_refresh_seconds:
                last_weather_fetch = started
                outdoor = await fetch_outdoor_temp(http, cfg) or outdoor
            fresh = outdoor is not None and now - outdoor[1] < timedelta(hours=cfg.weather_max_age_hours)
            outdoor_f = outdoor[0] if fresh else cfg.fallback_outdoor_temp_f

            # Room readings, and the humidity limit (which depends on room temperature).
            try:
                indoor_f = sensor.temperature * 9 / 5 + 32
                rh = sensor.relative_humidity
                max_rh, glass_f = humidity_limit(indoor_f, outdoor_f, cfg)
            except Exception as e:
                log.warning("Sensor read failed: %s", e)
                indoor_f = rh = max_rh = glass_f = None

            # Switch the plug. No sensor reading means off, to be safe.
            is_on = await plug.is_on()
            action = "NONE"
            if is_on is not None:
                want = should_be_on(is_on, rh, max_rh, cfg) if rh is not None else False
                if want != is_on and await plug.switch(want):
                    is_on = want
                    action = "ACTIVATED" if want else "DEACTIVATED"

            limits = "–" if max_rh is None else (
                f"{max_rh:.1f}% (on ≤{max_rh - cfg.on_margin:.1f}, off ≥{max_rh - cfg.off_margin:.1f})")
            log.info(
                "Out %s%s | In %s, %s | Glass %s | Max %s | Plug %s | %s",
                fmt(outdoor_f, "°F"), "" if fresh else " (fallback)",
                fmt(indoor_f, "°F"), fmt(rh, "%"), fmt(glass_f, "°F"), limits,
                {True: "ON", False: "OFF", None: "missing"}[is_on], action,
            )
            store.log({
                "timestamp": now.isoformat(timespec="seconds"),
                "outdoor_temp_f": round(outdoor_f, 2),
                "indoor_temp_f": round(indoor_f, 2) if indoor_f is not None else None,
                "indoor_rh": round(rh, 2) if rh is not None else None,
                "target_max_rh": round(max_rh, 2) if max_rh is not None else None,
                "humidifier_state": is_on,
                "action_taken": action,
            })
            status.__dict__.update(
                time=now, indoor_rh=rh, indoor_temp_f=indoor_f, outdoor_temp_f=outdoor_f,
                outdoor_observed_at=outdoor[1] if fresh else None,
                outdoor_is_fallback=not fresh, max_rh=max_rh, glass_temp_f=glass_f,
                humidifier_on=is_on,
            )

            wait = max(0.0, cfg.poll_seconds - (time.monotonic() - started))
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except TimeoutError:
                pass


def fmt(value: float | None, unit: str) -> str:
    return "–" if value is None else f"{value:.1f}{unit}"


# ==============================================================================
# Dashboard
# ==============================================================================

CSS = """
:root { color-scheme: light; }
body { margin: 0; background: #fff; color: #1f2328; line-height: 1.5;
  font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif; }
main { max-width: 760px; margin: 0 auto; padding: 32px 16px; }
h1, h2 { font-weight: 600; padding-bottom: .3em; border-bottom: 1px solid #d1d9e0; margin: 24px 0 16px; }
h1 { font-size: 2em; margin-top: 0; }
h2 { font-size: 1.5em; }
p { margin: 0 0 16px; }
table { border-collapse: collapse; padding: 0; word-break: initial; display: block;
  width: max-content; max-width: 100%; overflow: auto; margin: 0 auto 16px; }
tr { border: 1px solid #dfe2e5; margin: 0; padding: 0; }
tr:nth-child(2n), thead { background-color: #f8f8f8; }
th { font-weight: bold; border: 1px solid #dfe2e5; border-bottom: 0; margin: 0; padding: 6px 13px; text-align: left; }
td { border: 1px solid #dfe2e5; margin: 0; padding: 6px 13px; }
.muted { color: #59636e; font-size: 14px; }
figure { margin: 24px 0 36px; text-align: center; }
figcaption { font-weight: 600; font-size: 1.1em; margin-bottom: 8px; }
figure svg { display: block; width: 100%; max-width: 640px; height: auto; margin: 0 auto; }
.legend { margin-top: 8px; font-size: 14px; color: #59636e; }
.key { display: inline-block; margin: 0 10px; white-space: nowrap; }
.key i { display: inline-block; width: 16px; height: 0; border-top: 3px solid var(--c);
  vertical-align: middle; margin-right: 6px; }
.key i.dash { border-top-style: dashed; border-top-width: 2px; }
.key i.band { height: 12px; border: 0; background: var(--c); opacity: .2; }
"""

# Chart colors.
HUMIDITY, LIMIT, ON_BAND = "#3b82f6", "#ef4444", "#14b8a6"
ROOM_TEMP, OUTDOOR_TEMP = "#f97316", "#10b981"


def nice_step(span: float) -> float:
    """A round grid step (1, 2, 5 × 10^n) giving about 4 gridlines."""
    raw = max(span, 1e-9) / 4
    mag = 10 ** math.floor(math.log10(raw))
    return next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)


def svg_chart(start: datetime, end: datetime, series: list, unit: str, bands=(), gap=timedelta(minutes=5)) -> str:
    """Line chart as inline SVG. series = [(times, values, color, style)], style is "fill", "dash" or ""."""
    W, H, L, R, T, B = 560, 200, 42, 12, 10, 24
    values = [v for _, vs, _, _ in series for v in vs if v is not None]
    if not values:
        return '<p class="muted">No data yet.</p>'
    step = nice_step(max(values) - min(values))
    lo, hi = math.floor(min(values) / step) * step, math.ceil(max(values) / step) * step
    if hi == lo:
        hi = lo + step
    span = (end - start).total_seconds()
    bottom = H - B

    def x(t):
        return L + (t - start).total_seconds() / span * (W - L - R)

    def y(v):
        return T + (hi - v) / (hi - lo) * (bottom - T)

    out = [f'<svg viewBox="0 0 {W} {H}" font-size="12" fill="#6e7781">']
    for a, b in bands:
        out.append(f'<rect x="{x(a):.1f}" y="{T}" width="{max(x(b) - x(a), 1):.1f}" '
                   f'height="{bottom - T}" fill="{ON_BAND}" fill-opacity=".15"/>')
    v = lo
    while v <= hi + step / 2:
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="#eef0f3"/>')
        out.append(f'<text x="{L - 8}" y="{y(v) + 4:.1f}" text-anchor="end">{v:g}{unit}</text>')
        v += step
    out.append(f'<line x1="{L}" x2="{W - R}" y1="{bottom}" y2="{bottom}" stroke="#d0d7de"/>')
    t = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while t <= end:
        if t.hour % 6 == 0:
            out.append(f'<text x="{x(t):.1f}" y="{H - 5}" text-anchor="middle">{t.strftime("%-I %p")}</text>')
        t += timedelta(hours=1)

    for times, vs, color, style in series:
        # Split into segments wherever data is missing.
        segs, prev = [], None
        for t, v in zip(times, vs):
            if v is None:
                prev = None
                continue
            if prev is None or t - prev > gap:
                segs.append([])
            segs[-1].append((x(t), y(v)))
            prev = t
        if style == "fill":
            grad = f"g{color[1:]}"
            out.append(f'<defs><linearGradient id="{grad}" x1="0" y1="0" x2="0" y2="1">'
                       f'<stop offset="0" stop-color="{color}" stop-opacity=".25"/>'
                       f'<stop offset="1" stop-color="{color}" stop-opacity="0"/></linearGradient></defs>')
            for seg in segs:
                pts = "L".join(f"{px:.1f},{py:.1f}" for px, py in seg)
                out.append(f'<path d="M{seg[0][0]:.1f},{bottom}L{pts}L{seg[-1][0]:.1f},{bottom}Z" fill="url(#{grad})"/>')
        dash = ' stroke-dasharray="6 4" stroke-width="1.5"' if style == "dash" else ' stroke-width="2"'
        d = "".join("M" + "L".join(f"{px:.1f},{py:.1f}" for px, py in seg) for seg in segs)
        out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-linejoin="round" stroke-linecap="round"{dash}/>')
    out.append("</svg>")
    return "".join(out)


def figure(title: str, chart: str, *keys: tuple[str, str, str]) -> str:
    """A centered chart with its title and legend. keys = [(label, color, "" | "dash" | "band")]."""
    legend = "".join(f'<span class="key"><i class="{kind}" style="--c:{color}"></i>{label}</span>'
                     for label, color, kind in keys)
    return f'<figure><figcaption>{title}</figcaption>{chart}<div class="legend">{legend}</div></figure>'


def local_time(t: datetime) -> str:
    return t.astimezone().strftime("%-I:%M %p")


def render_page(s: Status, rows: list[tuple], cfg: Config) -> str:
    end = datetime.now().astimezone()
    start = end - timedelta(hours=24)

    if s.time is None:
        summary = '<p class="muted">Starting up, no readings yet.</p>'
    else:
        plug = {True: "<strong>ON</strong>", False: "OFF", None: "Plug not found"}[s.humidifier_on]
        if s.outdoor_is_fallback:
            outdoor = f"{fmt(s.outdoor_temp_f, '°F')} (no recent reading from {escape(cfg.nws_station)}, using fallback)"
        else:
            outdoor = f"{fmt(s.outdoor_temp_f, '°F')} ({escape(cfg.nws_station)}, as of {local_time(s.outdoor_observed_at)})"
        limit = "–" if s.max_rh is None else (
            f"{s.max_rh:.1f}% (turns on below {s.max_rh - cfg.on_margin:.1f}%, "
            f"off at {s.max_rh - cfg.off_margin:.1f}%)")
        summary = (
            f'<p class="muted">Updated {local_time(s.time)} · refreshes every minute</p><table>'
            f"<tr><th>Room humidity</th><td>{fmt(s.indoor_rh, '%')}</td></tr>"
            f"<tr><th>Room temperature</th><td>{fmt(s.indoor_temp_f, '°F')}</td></tr>"
            f"<tr><th>Humidifier</th><td>{plug}</td></tr>"
            f"<tr><th>Outdoor temperature</th><td>{outdoor}</td></tr>"
            f"<tr><th>Window edge (estimate)</th><td>{fmt(s.glass_temp_f, '°F')}</td></tr>"
            f"<tr><th>Max humidity</th><td>{limit}</td></tr></table>"
        )

    times = [datetime.fromisoformat(r[0]).astimezone() for r in rows]
    col = lambda i: [r[i] for r in rows]  # noqa: E731
    bands, band_start, gap = [], None, timedelta(seconds=cfg.poll_seconds * 3)
    for i, (t, on) in enumerate(zip(times, col(5))):
        if on and band_start is None:
            band_start = t
        broken = i + 1 == len(times) or times[i + 1] - t > gap or not col(5)[i + 1]
        if band_start is not None and broken:
            bands.append((band_start, t + timedelta(seconds=cfg.poll_seconds)))
            band_start = None

    humidity = figure(
        "Humidity",
        svg_chart(start, end, [(times, col(1), HUMIDITY, "fill"), (times, col(4), LIMIT, "dash")], "%", bands, gap),
        ("Room", HUMIDITY, ""), ("Max", LIMIT, "dash"), ("Humidifier on", ON_BAND, "band"),
    )
    temps = figure(
        "Temperature",
        svg_chart(start, end, [(times, col(2), ROOM_TEMP, ""), (times, col(3), OUTDOOR_TEMP, "")], "°F", (), gap),
        ("Room", ROOM_TEMP, ""), ("Outdoor", OUTDOOR_TEMP, ""),
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Humidistat</title>
<style>{CSS}</style>
</head>
<body><main>
<h1>Humidistat</h1>
{summary}
<h2>Last 24 hours</h2>
{humidity}
{temps}
</main></body>
</html>"""


def make_app(status: Status, store: Store, cfg: Config) -> web.Application:
    async def page(_):
        rows = store.history(datetime.now(timezone.utc) - timedelta(hours=24))
        return web.Response(text=render_page(status, rows, cfg), content_type="text/html")

    async def api_status(_):
        return web.json_response(asdict(status), dumps=lambda o: json.dumps(o, default=str))

    app = web.Application()
    app.add_routes([web.get("/", page), web.get("/api/status", api_status)])
    return app


# ==============================================================================
# Startup
# ==============================================================================


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "config.toml"
    cfg = load_config(config_path)
    store = Store(config_path.parent / cfg.db_path)

    try:
        sensor = open_sensor()
    except Exception as e:
        log.critical("Can't open the HDC302x sensor on I2C: %s", e)
        sys.exit(1)

    status = Status()
    plug = Plug(cfg.plug_alias, store)

    runner = web.AppRunner(make_app(status, store, cfg), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, cfg.dashboard_host, cfg.dashboard_port).start()
    log.info("Dashboard at http://%s:%d", cfg.dashboard_host, cfg.dashboard_port)

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)

    try:
        await run(cfg, sensor, plug, store, status, stop)
    finally:
        # Don't leave the humidifier running unattended.
        if plug.dev is not None:
            log.info("Shutting down, turning humidifier off")
            await plug.switch(False)
            await plug.drop()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
