# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6>=6.7"]
# ///
"""Humidistat Viewer: a desktop window showing the humidistat's readings and history.

Reads /api/status and /api/history from the humidistat on the Pi.

    uv run viewer.py                          # last server used, or raspberrypi.local:8080
    uv run viewer.py --url http://pi.lan:8080 # remembered for next time
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
import time
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, QRectF, QSettings, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QKeySequence, QPainter, QPainterPath, QPalette, QPen, QShortcut
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QToolTip, QVBoxLayout, QWidget,
)

DEFAULT_URL = "http://raspberrypi.local:8080"
REFRESH_MS = 60_000
STALE_SECONDS = 300  # warn when the newest reading is older than this
RANGES = [("6 hours", "6"), ("24 hours", "24"), ("7 days", "168"), ("30 days", "720"), ("All", "all")]

# Series colors, checked for color-blind separation within each chart, on light and dark backgrounds.
COLORS = {
    "light": {"humidity": "#2563eb", "limit": "#dc2626", "on": "#0d9488", "room": "#ea580c", "outdoor": "#7c3aed"},
    "dark": {"humidity": "#3b82f6", "limit": "#ef4444", "on": "#0d9488", "room": "#ea580c", "outdoor": "#8b5cf6"},
}


def series_color(widget: QWidget, name: str) -> QColor:
    dark = widget.palette().color(QPalette.ColorRole.Base).lightness() < 128
    return QColor(COLORS["dark" if dark else "light"][name])


# ==============================================================================
# Formatting
# ==============================================================================


def fmt(v: float | None, unit: str = "", digits: int = 1) -> str:
    return "–" if v is None else f"{v:.{digits}f}{unit}"


def hour_label(d: datetime) -> str:
    return f"{d.hour % 12 or 12} {'AM' if d.hour < 12 else 'PM'}"


def clock(d: datetime) -> str:
    return f"{d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def when(ts: float, with_date: bool) -> str:
    d = datetime.fromtimestamp(ts)
    return f"{d:%a %b} {d.day}, {clock(d)}" if with_date else f"{d:%a} {clock(d)}"


def dew_point_f(temp_f: float, rh: float) -> float:
    c = (temp_f - 32) * 5 / 9
    g = math.log(max(rh, 0.1) / 100) + 17.62 * c / (243.12 + c)
    return 243.12 * g / (17.62 - g) * 9 / 5 + 32


def parse_time(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s).astimezone() if s and s != "None" else None


# ==============================================================================
# Network
# ==============================================================================


class Api(QObject):
    """Fetches JSON from the humidistat without blocking the window."""

    status = Signal(dict)
    history = Signal(str, dict)  # range key, data
    failed = Signal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url.rstrip("/")
        self.nam = QNetworkAccessManager(self)

    def fetch_status(self) -> None:
        self._get("/api/status", self.status.emit)

    def fetch_history(self, hours: str) -> None:
        self._get(f"/api/history?hours={hours}", lambda d: self.history.emit(hours, d))

    def _get(self, path: str, done) -> None:
        req = QNetworkRequest(QUrl(self.url + path))
        req.setTransferTimeout(15_000)
        reply = self.nam.get(req)

        def finished():
            reply.deleteLater()
            if reply.error() != QNetworkReply.NetworkError.NoError:
                self.failed.emit(reply.errorString())
                return
            try:
                data = json.loads(reply.readAll().data())
            except ValueError as e:
                self.failed.emit(f"Bad response from {path}: {e}")
                return
            done(data)

        reply.finished.connect(finished)


# ==============================================================================
# Chart
# ==============================================================================


def nice_step(span: float, ticks: int = 4) -> float:
    raw = max(span, 1e-9) / ticks
    mag = 10 ** math.floor(math.log10(raw))
    return next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)


def time_ticks(start: float, end: float) -> list[tuple[float, str]]:
    """Gridline times and labels, on round local hours, days or months."""
    hour, day = 3600, 86400
    span = end - start
    step = next((s for s in (hour, 2 * hour, 3 * hour, 6 * hour, 12 * hour, day, 2 * day, 7 * day)
                 if span / s <= 8), 30 * day)
    t = datetime.fromtimestamp(start)
    ticks = []
    if step >= 30 * day:
        t = t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while t.timestamp() <= end:
            t = t.replace(year=t.year + (t.month == 12), month=t.month % 12 + 1)
            ticks.append((t.timestamp(), f"{t:%b}"))
    elif step >= day:
        t = t.replace(hour=0, minute=0, second=0, microsecond=0)
        while (t := t + timedelta(days=step // day)).timestamp() <= end:
            ticks.append((t.timestamp(), f"{t:%b} {t.day}"))
    else:
        t = t.replace(minute=0, second=0, microsecond=0)
        while (t := t + timedelta(hours=1)).timestamp() <= end:
            if t.hour % (step // hour) == 0:
                ticks.append((t.timestamp(), f"{t:%a}" if t.hour == 0 else hour_label(t)))
    return [tk for tk in ticks if tk[0] <= end]


class Chart(QWidget):
    """Time-series line chart drawn with the system palette, with a shared hover crosshair.

    series: [(data key, legend label, color name, dashed)]
    """

    hovered = Signal(object)  # time under the mouse (epoch seconds), or None

    def __init__(self, unit: str, series: list[tuple[str, str, str, bool]], humidity: bool = False):
        super().__init__()
        self.unit, self.series, self.humidity = unit, series, humidity
        self.points: list[dict] = []
        self.times: list[float] = []
        self.start, self.end, self.bucket = 0.0, 1.0, 60
        self.hover: float | None = None
        self.setMouseTracking(True)
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def sizeHint(self) -> QSize:
        return QSize(600, 240)

    def set_data(self, data: dict) -> None:
        self.points = data["points"]
        self.times = [p["t"] for p in self.points]
        self.start, self.end, self.bucket = data["start"], data["end"], data["bucket_seconds"]
        self.update()

    def set_hover(self, t: float | None) -> None:
        if t != self.hover:
            self.hover = t
            self.update()

    # --- geometry -----------------------------------------------------------

    @property
    def gap(self) -> float:
        """Points further apart than this aren't joined (the sensor or service was down)."""
        return max(180, 2.5 * self.bucket)

    def plot_rect(self) -> QRectF:
        fm = QFontMetricsF(self.font())
        left = fm.horizontalAdvance(f"100{self.unit}") + 10
        top = fm.height() + 12  # room for the legend
        return QRectF(left, top, max(self.width() - left - 12, 1), max(self.height() - top - fm.height() - 10, 1))

    def y_range(self) -> tuple[float, float, float]:
        keys = [k for k, *_ in self.series] + (["rh_lo", "rh_hi"] if self.humidity else [])
        vals = [p[k] for p in self.points for k in keys if p.get(k) is not None]
        if not vals:
            return 0, 100, 25
        step = nice_step(max(vals) - min(vals))
        lo, hi = math.floor(min(vals) / step) * step, math.ceil(max(vals) / step) * step
        return lo, (hi if hi > lo else lo + step), step

    def nearest(self, t: float) -> int | None:
        if not self.times:
            return None
        i = bisect.bisect_left(self.times, t)
        i = min((j for j in (i - 1, i) if 0 <= j < len(self.times)), key=lambda j: abs(self.times[j] - t))
        return i if abs(self.times[i] - t) <= self.gap else None

    # --- mouse --------------------------------------------------------------

    def mouseMoveEvent(self, e) -> None:
        r = self.plot_rect()
        x = e.position().x()
        t = self.start + (x - r.left()) / r.width() * (self.end - self.start) if r.left() <= x <= r.right() else None
        self.set_hover(t)
        self.hovered.emit(t)
        i = self.nearest(t) if t is not None else None
        if i is None:
            QToolTip.hideText()
        else:
            QToolTip.showText(e.globalPosition().toPoint(), self.tooltip(self.points[i]), self)

    def leaveEvent(self, _) -> None:
        self.set_hover(None)
        self.hovered.emit(None)
        QToolTip.hideText()

    def tooltip(self, p: dict) -> str:
        averaged = self.bucket > 90
        lines = [f"<b>{when(p['t'], self.end - self.start > 6 * 86400)}</b>"]
        for key, label, _, _ in self.series:
            lines.append(f"{label}: {fmt(p.get(key), self.unit)}")
        if self.humidity:
            if averaged and p.get("rh_lo") is not None:
                lines.append(f"Range: {p['rh_lo']:.1f}–{p['rh_hi']:.1f}%")
            if p.get("on") is not None:
                lines.append(f"Humidifier on {p['on']:.0%} of the time" if averaged
                             else f"Humidifier {'on' if p['on'] else 'off'}")
        if averaged:
            lines.append(f"<i>average of {self.bucket / 60:.0f} min</i>")
        return "<br>".join(lines)

    # --- drawing ------------------------------------------------------------

    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        text = pal.color(QPalette.ColorRole.WindowText)
        muted = pal.color(QPalette.ColorRole.PlaceholderText)
        grid = QColor(muted)
        grid.setAlphaF(0.25)
        surface = pal.color(QPalette.ColorRole.Window)
        fm = QFontMetricsF(self.font())
        r = self.plot_rect()

        self.draw_legend(p, fm, r, text)
        if not self.points:
            p.setPen(muted)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "No data for this range")
            return

        lo, hi, step = self.y_range()
        span = self.end - self.start

        def x(t):
            return r.left() + (t - self.start) / span * r.width()

        def y(v):
            return r.bottom() - (v - lo) / (hi - lo) * r.height()

        # Humidifier-on shading, darker where it ran more of the time.
        if self.humidity:
            on = series_color(self, "on")
            runs: list[list] = []  # [first t, last t, fraction]
            for pt in self.points:
                f = round(pt.get("on") or 0, 1)
                if runs and runs[-1][2] == f and pt["t"] - runs[-1][1] <= self.gap:
                    runs[-1][1] = pt["t"]
                else:
                    runs.append([pt["t"], pt["t"], f])
            for a, b, f in runs:
                if f > 0:
                    c = QColor(on)
                    c.setAlphaF(0.22 * f)
                    half = self.bucket / 2
                    p.fillRect(QRectF(x(a - half), r.top(), max(x(b + half) - x(a - half), 1), r.height()), c)

        # Grid and axes.
        p.setPen(QPen(grid, 1))
        v = lo
        while v <= hi + step / 2:
            p.drawLine(r.left(), y(v), r.right(), y(v))
            v += step
        p.setPen(muted)
        v = lo
        while v <= hi + step / 2:
            p.drawText(QRectF(0, y(v) - fm.height() / 2, r.left() - 8, fm.height()),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{v:g}{self.unit}")
            v += step
        last_right = -math.inf
        for t, label in time_ticks(self.start, self.end):
            w = fm.horizontalAdvance(label)
            if x(t) - w / 2 > last_right + 8 and x(t) + w / 2 <= self.width():
                p.drawText(QRectF(x(t) - w / 2, r.bottom() + 4, w, fm.height()), Qt.AlignmentFlag.AlignCenter, label)
                last_right = x(t) + w / 2

        # Min–max band behind the averaged humidity line.
        if self.humidity and self.bucket > 90:
            band = series_color(self, "humidity")
            band.setAlphaF(0.15)
            for seg in self.segments("rh_lo"):
                path = QPainterPath()
                path.moveTo(x(seg[0]["t"]), y(seg[0]["rh_hi"]))
                for pt in seg[1:]:
                    path.lineTo(x(pt["t"]), y(pt["rh_hi"]))
                for pt in reversed(seg):
                    path.lineTo(x(pt["t"]), y(pt["rh_lo"]))
                p.fillPath(path, band)

        # Lines.
        for key, _, cname, dashed in self.series:
            pen = QPen(series_color(self, cname), 1.5 if dashed else 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            if dashed:
                pen.setDashPattern([4, 3])
            p.setPen(pen)
            for seg in self.segments(key):
                path = QPainterPath()
                path.moveTo(x(seg[0]["t"]), y(seg[0][key]))
                for pt in seg[1:]:
                    path.lineTo(x(pt["t"]), y(pt[key]))
                p.drawPath(path)

        # Hover crosshair, with a dot on each line.
        i = self.nearest(self.hover) if self.hover is not None else None
        if i is not None:
            pt = self.points[i]
            p.setPen(QPen(muted, 1))
            p.drawLine(x(pt["t"]), r.top(), x(pt["t"]), r.bottom())
            for key, _, cname, _ in self.series:
                if pt.get(key) is not None:
                    p.setPen(QPen(surface, 2))
                    p.setBrush(series_color(self, cname))
                    p.drawEllipse(QRectF(x(pt["t"]) - 4.5, y(pt[key]) - 4.5, 9, 9))

    def segments(self, key: str) -> list[list[dict]]:
        """Runs of points with a value, split at missing data."""
        segs, prev = [], None
        for pt in self.points:
            if pt.get(key) is None:
                prev = None
                continue
            if prev is None or pt["t"] - prev > self.gap:
                segs.append([])
            segs[-1].append(pt)
            prev = pt["t"]
        return segs

    def draw_legend(self, p: QPainter, fm: QFontMetricsF, r: QRectF, text: QColor) -> None:
        items = [(label, cname, "dash" if dashed else "line") for _, label, cname, dashed in self.series]
        if self.humidity:
            items.append(("Humidifier on", "on", "band"))
        xpos, ymid = r.left(), fm.height() / 2 + 2
        for label, cname, kind in items:
            c = series_color(self, cname)
            if kind == "band":
                c.setAlphaF(0.35)
                p.fillRect(QRectF(xpos, ymid - 6, 16, 12), c)
            else:
                pen = QPen(c, 1.5 if kind == "dash" else 2)
                if kind == "dash":
                    pen.setDashPattern([3, 2])
                p.setPen(pen)
                p.drawLine(xpos, ymid, xpos + 16, ymid)
            p.setPen(text)
            w = fm.horizontalAdvance(label)
            p.drawText(QRectF(xpos + 22, 2, w + 2, fm.height()), Qt.AlignmentFlag.AlignVCenter, label)
            xpos += 22 + w + 18


# ==============================================================================
# Window
# ==============================================================================


class Readout(QGroupBox):
    """A titled box of label: value rows."""

    def __init__(self, title: str, rows: list[str]):
        super().__init__(title)
        form = QFormLayout(self)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.values: dict[str, QLabel] = {}
        for name in rows:
            v = QLabel("–")
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            v.setWordWrap(True)
            form.addRow(name + ":", v)
            self.values[name] = v

    def set(self, name: str, value: str) -> None:
        self.values[name].setText(value)


class Window(QWidget):
    def __init__(self, url: str, settings: QSettings):
        super().__init__()
        self.settings = settings
        self.setWindowTitle("Humidistat")
        self.api = Api(url)
        self.range = settings.value("range", "24")
        if self.range not in dict((k, l) for l, k in RANGES):
            self.range = "24"
        self.newest: datetime | None = None
        self.error: str | None = None

        # Top row: a notice (only when something's wrong) and the range buttons.
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.hide()
        self.buttons = QButtonGroup(self)
        top = QHBoxLayout()
        top.addWidget(self.notice, 1)
        top.addStretch()
        for n, (label, key) in enumerate(RANGES, start=1):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(key == self.range)
            b.setToolTip(f"Ctrl+{n}" if sys.platform != "darwin" else f"⌘{n}")
            self.buttons.addButton(b)
            b.clicked.connect(lambda _=False, k=key: self.set_range(k))
            QShortcut(QKeySequence(f"Ctrl+{n}"), self, activated=b.click)
            top.addWidget(b)
        QShortcut(QKeySequence.StandardKey.Refresh, self, activated=self.refresh)

        self.now = Readout("Now", ["Room humidity", "Max humidity", "Humidifier", "Room temperature",
                                   "Room dew point", "Window edge (est.)", "Outdoor temperature"])
        self.summary = Readout("", ["Average humidity", "Humidity range", "Above max", "Humidifier on",
                                    "Humidifier starts", "Outdoor range"])
        boxes = QHBoxLayout()
        boxes.addWidget(self.now, 1)
        boxes.addWidget(self.summary, 1)

        self.humidity = Chart("%", [("rh", "Room", "humidity", False), ("max_rh", "Max", "limit", True)], humidity=True)
        self.temps = Chart("°F", [("temp", "Room", "room", False), ("outdoor", "Outdoor", "outdoor", False)])
        self.humidity.hovered.connect(self.temps.set_hover)
        self.temps.hovered.connect(self.humidity.set_hover)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(boxes)
        for title, chart in (("Humidity", self.humidity), ("Temperature", self.temps)):
            box = QGroupBox(title)
            QVBoxLayout(box).addWidget(chart)
            layout.addWidget(box, 1)

        self.api.status.connect(self.show_status)
        self.api.history.connect(self.show_history)
        self.api.failed.connect(self.show_error)
        self.timer = QTimer(self, interval=REFRESH_MS, timeout=self.refresh)
        self.timer.start()
        self.update_summary_title()

        geometry = settings.value("geometry")
        if geometry is None or not self.restoreGeometry(geometry):
            self.resize(960, 860)
        self.refresh()

    def closeEvent(self, e) -> None:
        self.settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(e)

    # --- data ---------------------------------------------------------------

    def refresh(self) -> None:
        self.error = None
        self.api.fetch_status()
        self.api.fetch_history(self.range)

    def set_range(self, key: str) -> None:
        if key != self.range:
            self.range = key
            self.settings.setValue("range", key)
            self.update_summary_title()
            self.api.fetch_history(key)

    def update_summary_title(self) -> None:
        label = dict((k, l) for l, k in RANGES)[self.range]
        self.summary.setTitle("All history" if self.range == "all" else f"Last {label}")

    def show_status(self, s: dict) -> None:
        rh, temp, max_rh = s.get("indoor_rh"), s.get("indoor_temp_f"), s.get("max_rh")
        on_at = off_at = None
        if max_rh is not None:
            on_at, off_at = max_rh - s.get("on_margin", 5), max_rh - s.get("off_margin", 1)

        self.now.set("Room humidity", fmt(rh, "%") + (" (above max)" if None not in (rh, max_rh) and rh > max_rh else ""))
        self.now.set("Max humidity", fmt(max_rh, "%"))
        plug = {True: "On", False: "Off", None: "Plug not found"}[s.get("humidifier_on")]
        if on_at is not None:
            plug += f" (turns on below {on_at:.1f}%, off at {off_at:.1f}%)"
        self.now.set("Humidifier", plug)
        self.now.set("Room temperature", fmt(temp, "°F"))
        dew = dew_point_f(temp, rh) if None not in (temp, rh) else None
        self.now.set("Room dew point", fmt(dew, "°F"))
        glass = s.get("glass_temp_f")
        margin = f" ({glass - dew:.1f}°F above dew point)" if None not in (glass, dew) else ""
        self.now.set("Window edge (est.)", fmt(glass, "°F") + margin)
        station = s.get("nws_station", "NWS")
        observed = parse_time(s.get("outdoor_observed_at"))
        if s.get("outdoor_is_fallback"):
            outdoor = f"{fmt(s.get('outdoor_temp_f'), '°F')} (no recent {station} reading, using fallback)"
        else:
            outdoor = fmt(s.get("outdoor_temp_f"), "°F") + (f" ({station}, {clock(observed)})" if observed else "")
        self.now.set("Outdoor temperature", outdoor)

        self.newest = parse_time(s.get("time"))
        self.update_notice()

    def show_history(self, key: str, data: dict) -> None:
        if key != self.range:
            return  # a reply for a range the user already left
        self.humidity.set_data(data)
        self.temps.set_data(data)
        sm = data["summary"]
        pct = lambda f: "–" if f is None else f"{f:.0%} of the time"  # noqa: E731
        self.summary.set("Average humidity", fmt(sm["rh_avg"], "%"))
        self.summary.set("Humidity range", "–" if sm["rh_min"] is None else f"{sm['rh_min']:.1f}–{sm['rh_max']:.1f}%")
        self.summary.set("Above max", pct(sm["above_max_fraction"]))
        on = pct(sm["on_fraction"])
        if sm["on_fraction"]:
            on += f" ({sm['on_hours']:g} h)"
        self.summary.set("Humidifier on", on)
        self.summary.set("Humidifier starts", f"{sm['activations']:,}")
        self.summary.set("Outdoor range",
                         "–" if sm["outdoor_min"] is None else f"{sm['outdoor_min']:.1f}–{sm['outdoor_max']:.1f}°F")

    def show_error(self, message: str) -> None:
        self.error = message
        self.update_notice()

    def update_notice(self) -> None:
        """Only says anything when something is wrong."""
        if self.error:
            text = f"Can't reach the humidistat at {self.api.url}: {self.error}"
        elif self.newest and time.time() - self.newest.timestamp() > STALE_SECONDS:
            text = f"No new readings since {when(self.newest.timestamp(), True)}."
        else:
            text = ""
        self.notice.setText(text)
        self.notice.setVisible(bool(text))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", help=f"humidistat address (default: last used, or {DEFAULT_URL})")
    args = ap.parse_args()

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Humidistat")
    settings = QSettings("humidistat", "viewer")
    if args.url:
        settings.setValue("url", args.url)
    window = Window(settings.value("url", DEFAULT_URL), settings)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
