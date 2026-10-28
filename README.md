# humidistat

Keeps a room's humidity below a limit that depends on the outdoor temperature, so
windows don't fog up in winter. Runs on a Raspberry Pi.

- Reads temperature and humidity from an Adafruit HDC302x sensor over I2C.
- Gets the outdoor temperature from the National Weather Service.
- Switches a humidifier on a Kasa smart plug, found on the network by name.
- Logs every reading to SQLite.
- Serves a status page with 24-hour charts at `http://<pi>:8080` (JSON at `/api/status`).

## Setup

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/SheinH/humidistat ~/humidistat
cd ~/humidistat
cp config.example.toml config.toml   # optional, edit as needed
uv run humidistat.py                 # try it out
```

Run it as a service:

```sh
sudo cp humidistat.service /etc/systemd/system/
sudo systemctl enable --now humidistat
journalctl -u humidistat -f
```

Update: `git pull && sudo systemctl restart humidistat`.

## How it decides

The max humidity follows an S-curve from 30% (very cold outside) to 45% (warm).
The humidifier turns on when the room is 5 points below the max and off 1 point
below it. If the sensor fails it turns the humidifier off; if the plug can't be
found it keeps searching every minute.
