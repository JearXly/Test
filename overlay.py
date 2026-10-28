"""External telemetry overlay. Demo mode does not connect to Rust."""
import argparse
import json
import math
import socket
import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget


DEFAULTS = {
    "port": 27841,
    "fps": 60,
    "max_distance": 300,
    "color": "#45ffaa",
    "line_width": 2,
    "font_size": 12,
    "boxes": True,
    "names": True,
    "distance": True,
    "health": True,
    "snaplines": False,
    "crosshair": False,
    "stale_seconds": 1.0,
}


def entities_from_packet(data):
    """Accept bounded screen-space rectangles; no executable packet content."""
    packet = json.loads(data)
    result = []
    for item in packet["entities"][:512]:
        numbers = {k: float(item[k]) for k in ("x", "y", "w", "h", "distance", "health")}
        if not all(math.isfinite(v) for v in numbers.values()):
            continue
        if numbers["w"] <= 0 or numbers["h"] <= 0:
            continue
        result.append({**numbers, "name": str(item.get("name", "Entity"))[:80]})
    return result


class Overlay(QWidget):
    def __init__(self, config, demo):
        super().__init__()
        self.config_path = config
        self.demo = demo
        self.config = dict(DEFAULTS)
        self.last_config = None
        self.entities = []
        self.received_at = 0
        self.sock = None
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool |
                            Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setGeometry(QApplication.primaryScreen().geometry())
        self.reload_config()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(round(1000 / self.config["fps"]))
        self.reload_timer = QTimer(self)
        self.reload_timer.timeout.connect(self.reload_config)
        self.reload_timer.start(1000)

    def reload_config(self):
        try:
            contents = self.config_path.read_text() if self.config_path.exists() else "{}"
            if contents == self.last_config:
                return
            c = {**DEFAULTS, **json.loads(contents)}
            c["fps"] = max(10, min(144, int(c["fps"])))
            c["font_size"] = max(8, min(48, int(c["font_size"])))
            c["line_width"] = max(1, min(10, int(c["line_width"])))
            for key in ("max_distance", "stale_seconds"):
                c[key] = float(c[key])
                if not math.isfinite(c[key]) or c[key] <= 0:
                    raise ValueError(key + " must be finite and positive")
            if not QColor(c["color"]).isValid():
                raise ValueError("invalid color")
            port = int(c["port"])
            if not self.demo and (self.sock is None or port != self.config["port"]):
                replacement = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    replacement.bind(("127.0.0.1", port))
                    replacement.setblocking(False)
                except Exception:
                    replacement.close()
                    raise
                if self.sock:
                    self.sock.close()
                self.sock = replacement
            self.config = c
            self.last_config = contents
            if hasattr(self, "timer"):
                self.timer.setInterval(round(1000 / c["fps"]))
        except (OSError, ValueError, TypeError) as exc:
            print("Config unchanged:", exc, file=sys.stderr)

    def tick(self):
        if self.demo:
            self.entities = [{"x": self.width() / 2 + math.sin(time.monotonic()) * 150,
                              "y": self.height() / 3, "w": 80, "h": 180,
                              "name": "DEMO ENTITY", "distance": 42, "health": 75}]
            self.received_at = time.monotonic()
        elif self.sock:
            # Bound work each frame so a noisy producer cannot freeze rendering.
            for _ in range(32):
                try:
                    data, _ = self.sock.recvfrom(65507)
                    parsed = entities_from_packet(data)
                    self.entities = parsed
                    self.received_at = time.monotonic()
                except BlockingIOError:
                    break
                except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
                    continue
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = self.config
        color = QColor(c["color"])
        p.setFont(QFont("Segoe UI", c["font_size"]))
        p.setPen(QPen(color, c["line_width"]))
        p.drawText(20, 30, "DEMO — no Rust connection" if self.demo else "TELEMETRY OVERLAY")
        if c["crosshair"]:
            x, y = self.width() // 2, self.height() // 2
            p.drawLine(x - 6, y, x + 6, y)
            p.drawLine(x, y - 6, x, y + 6)
        if time.monotonic() - self.received_at > c["stale_seconds"]:
            p.drawText(20, 55, "Waiting for screen-space telemetry")
            return
        for e in self.entities:
            if e["distance"] > c["max_distance"]:
                continue
            x, y, w, h = (e[k] for k in ("x", "y", "w", "h"))
            p.setPen(QPen(color, c["line_width"]))
            if c["boxes"]:
                p.drawRect(QRectF(x, y, w, h))
            label = []
            if c["names"]:
                label.append(e["name"])
            if c["distance"]:
                label.append(f'{e["distance"]:.0f} m')
            p.drawText(QRectF(x - 100, y - 25, w + 200, 24), Qt.AlignCenter, " · ".join(label))
            if c["health"]:
                fraction = max(0, min(1, e["health"] / 100))
                p.fillRect(QRectF(x - 8, y, 4, h), QColor("#444444"))
                p.fillRect(QRectF(x - 8, y + h * (1 - fraction), 4, h * fraction), color)
            if c["snaplines"]:
                p.drawLine(int(self.width() / 2), self.height(), int(x + w / 2), int(y + h))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    window = Overlay(args.config, args.demo)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
