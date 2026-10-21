
import sys
import os
import json
import time
import math
import threading
import subprocess
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QPointF, Signal, QObject
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget, QLabel, QSystemTrayIcon, QMenu

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"

DEFAULT_CONFIG = {
    "brightness_step": 5,
    "volume_step": 5,
    "apps": [
        {"name": "Notepad", "command": "notepad.exe"},
        {"name": "Calculator", "command": "calc.exe"},
        {"name": "VS Code", "command": "code"},
    ],
    "links": [
        {"name": "YouTube", "url": "https://www.youtube.com"},
        {"name": "ChatGPT", "url": "https://chatgpt.com"},
        {"name": "GitHub", "url": "https://github.com"},
        {"name": "Google", "url": "https://www.google.com"},
    ],
}


def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, indent=2),
            encoding="utf-8",
        )
        return json.loads(json.dumps(DEFAULT_CONFIG))

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config.update(data)
        return config
    except Exception as exc:
        print(f"Could not read config.json: {exc}")
        return json.loads(json.dumps(DEFAULT_CONFIG))
def get_audio_endpoint_volume():
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    device = AudioUtilities.GetSpeakers()

    # Modern pycaw versions expose EndpointVolume directly.
    endpoint = getattr(device, "EndpointVolume", None)
    if endpoint is not None:
        return endpoint

    # Compatibility with versions exposing the raw COM device.
    interface = device.Activate(
        IAudioEndpointVolume._iid_,
        CLSCTX_ALL,
        None,
    )
    return cast(interface, POINTER(IAudioEndpointVolume))


class Events(QObject):
    command = Signal(str)


class AudioVolumeMonitor:
    """
    Poll the Windows master-volume endpoint.

    Volume changes caused by the CMF dial become rotation events.
    On pages other than Volume, the original volume is restored.
    """

    def __init__(self, callback, get_page, get_window):
        self.callback = callback
        self.get_page = get_page
        self.get_window = get_window

        self.running = False
        self.thread = None
        self.volume = None
        self.lock = threading.Lock()
        self.last_rotation = 0.0
        self.last_restore = 0.0
        self.restore_guard_until = 0.0
        self.threshold = 0.002
        self.poll_interval = 0.025

    def start(self):
        self.running = True
        self.thread = threading.Thread(
            target=self._worker,
            daemon=True,
            name="CMF-Audio-Monitor",
        )
        self.thread.start()

    def stop(self):
        self.running = False

    def _connect_audio(self):
        return get_audio_endpoint_volume()

    def _worker(self):
        initialized_com = False

        try:
            import comtypes

            comtypes.CoInitialize()
            initialized_com = True
            self.volume = self._connect_audio()

            previous = float(
                self.volume.GetMasterVolumeLevelScalar()
            )

            print(
                f"[AUDIO] Monitoring Windows volume. "
                f"Initial volume: {previous * 100:.0f}%",
                flush=True,
            )

        except Exception as exc:
            print(
                "[AUDIO ERROR] Windows volume monitoring is unavailable.\n"
                f"Reason: {exc}\n"
                "Install pycaw and comtypes, then restart the controller.",
                flush=True,
            )
            self.running = False
            if initialized_com:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass
            return

        try:
            while self.running:
                try:
                    current = float(
                        self.volume.GetMasterVolumeLevelScalar()
                    )
                    now = time.monotonic()
                    delta = current - previous

                    if abs(delta) >= self.threshold:
                        # Ignore changes caused by our own restoration.
                        if now < self.restore_guard_until:
                            previous = current

                        else:
                            page = self.get_page()
                            menu_visible = self.get_window().isVisible()

                            # Volume page: leave hardware volume changes alone.
                            if menu_visible and page == "volume":
                                previous = current

                            elif menu_visible:
                                direction = 1 if delta > 0 else -1

                                # Restore the volume first so the dial acts
                                # as a controller on non-volume pages.
                                try:
                                    self.restore_guard_until = now + 0.18
                                    self.volume.SetMasterVolumeLevelScalar(
                                        previous, None
                                    )
                                    self.last_restore = now
                                except Exception as exc:
                                    print(
                                        f"[AUDIO WARNING] "
                                        f"Could not restore volume: {exc}",
                                        flush=True,
                                    )
                                    previous = current

                                # Avoid generating many navigation events
                                # from one physical dial movement.
                                if now - self.last_rotation >= 0.065:
                                    self.last_rotation = now
                                    self.callback(direction)

                                # Keep the old level as the baseline.
                                # The guard prevents the restoration from
                                # becoming another rotation event.

                            else:
                                # Menu closed: normal Windows volume control.
                                previous = current

                    else:
                        # Track ordinary volume changes while the menu
                        # is closed or the Volume page is active.
                        if now >= self.restore_guard_until:
                            previous = current

                except Exception as exc:
                    print(
                        f"[AUDIO WARNING] Volume read failed: {exc}",
                        flush=True,
                    )
                    time.sleep(0.25)

                time.sleep(self.poll_interval)

        finally:
            if initialized_com:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass


class RadialMenu(QWidget):
    def __init__(self, config, events):
        super().__init__()

        self.config = config
        self.events = events
        self.events.command.connect(self.handle_command)

        self.items = [
            ("Brightness", "brightness"),
            ("Volume", "volume"),
            ("Apps", "apps"),
            ("Links", "links"),
            ("Media", "media"),
            ("Settings", "settings"),
        ]

        self.page = "home"
        self.index = 0
        self.adjust_value = 50

        self.setWindowTitle("CMF Smart Dial")
        self.setFixedSize(440, 500)
        self.setWindowFlags(
        Qt.WindowType.FramelessWindowHint
    |   Qt.WindowType.WindowStaysOnTopHint
    |   Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.hint = QLabel(
            "DOUBLE PRESS: MENU  |  ROTATE: CONTROL  |  PRESS: SELECT",
            self,
        )
        self.hint.setGeometry(10, 395, 370, 24)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setStyleSheet("color:#9fb9d8;font-size:9px;")

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.update)
        self.refresh_timer.start(150)

        self._center_window()
        self.hide()

    def _center_window(self):
        screen = QApplication.primaryScreen()
        if screen is None:
            return

        area = screen.availableGeometry()
        self.move(
        area.x() + (area.width() - self.width()) // 2,
        area.y() + 10,
    )

    def show_menu(self):
        self.page = "home"
        self.index = 0
        self._center_window()
        self.show()
        self.raise_()
        self.activateWindow()
        self.update()

    def hide_menu(self):
        self.hide()

    def handle_command(self, command):
        if command == "toggle_menu":
            if self.isVisible():
                self.hide_menu()
            else:
                self.show_menu()
            return
        if command == "close_menu":
            self.hide_menu()
            return

        if not self.isVisible():
            return

        if command == "next":
            self._move(1)
        elif command == "previous":
            self._move(-1)
        elif command == "rotate_right":
            self._rotate(1)
        elif command == "rotate_left":
            self._rotate(-1)
        elif command == "select":
            self._select()
        elif command == "escape":
            if self.page != "home":
                self.page = "home"
                self.index = 0
                self.update()
            else:
                self.hide_menu()

    def _current_options(self):
        if self.page == "home":
            return self.items

        if self.page == "apps":
            apps = [
                (x["name"], "launch:" + x["command"])
                for x in self.config.get("apps", [])
                if isinstance(x, dict)
                and x.get("name")
                and x.get("command")
            ]
            return apps + [("Back", "back")]

        if self.page == "links":
            links = [
                (x["name"], "url:" + x["url"])
                for x in self.config.get("links", [])
                if isinstance(x, dict)
                and x.get("name")
                and x.get("url")
            ]
            return links + [("Back", "back")]

        if self.page == "media":
            return [
                ("Play / Pause", "media:play"),
                ("Next track", "media:next"),
                ("Previous track", "media:previous"),
                ("Back", "back"),
            ]

        if self.page == "settings":
            return [
                ("Lock Windows", "lock"),
                ("Open config file", "config"),
                ("Back", "back"),
            ]

        if self.page in ("brightness", "volume"):
            return [
                ("Decrease", "decrease"),
                ("Increase", "increase"),
                ("Back", "back"),
            ]

        return [("Back", "back")]

    def _move(self, step):
        options = self._current_options()
        if options:
            self.index = (self.index + step) % len(options)
            self.update()

    def _rotate(self, direction):
        if self.page == "brightness":
            step = int(self.config.get("brightness_step", 5))
            self._adjust_brightness(direction * step)

        elif self.page == "volume":
            # Hardware dial volume changes already adjust system volume.
            # Do not apply a second volume adjustment here.
            pass

        else:
            self._move(direction)

        self.update()

    def _select(self):
        options = self._current_options()
        if not options:
            return

        label, action = options[self.index % len(options)]

        if self.page == "home":
            self.page = action
            self.index = 0

            if action == "brightness":
                self.adjust_value = self._get_brightness()
            elif action == "volume":
                self.adjust_value = self._get_volume()

        elif action == "back":
            self.page = "home"
            self.index = 0

        elif action == "decrease":
            self._adjust(-1)

        elif action == "increase":
            self._adjust(1)

        elif action.startswith("launch:"):
            command = action[len("launch:"):]
            try:
                subprocess.Popen(command, shell=True)
            except Exception as exc:
                print(f"Could not launch app: {exc}", flush=True)

        elif action.startswith("url:"):
            try:
                webbrowser.open(action[len("url:"):])
            except Exception as exc:
                print(f"Could not open website: {exc}", flush=True)

        elif action.startswith("media:"):
            self._send_media_key(action[len("media:"):])

        elif action == "lock":
            try:
                subprocess.Popen([
                    "rundll32.exe",
                    "user32.dll,LockWorkStation",
                ])
            except Exception as exc:
                print(f"Could not lock Windows: {exc}", flush=True)

        elif action == "config":
            try:
                if not CONFIG_PATH.exists():
                    CONFIG_PATH.write_text(
                        json.dumps(DEFAULT_CONFIG, indent=2),
                        encoding="utf-8",
                    )
                os.startfile(str(CONFIG_PATH))
            except Exception as exc:
                print(f"Could not open config.json: {exc}", flush=True)

        self.update()

    @staticmethod
    def _send_media_key(key):
        try:
            import ctypes

            keys = {
                "play": 0xB3,       # VK_MEDIA_PLAY_PAUSE
                "next": 0xB0,       # VK_MEDIA_NEXT_TRACK
                "previous": 0xB1,   # VK_MEDIA_PREV_TRACK
            }

            vk = keys[key]
            user32 = ctypes.windll.user32
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, 2, 0)

        except Exception as exc:
            print(f"Media command failed: {exc}", flush=True)

    def _get_brightness(self):
        try:
            import screen_brightness_control as sbc
            value = sbc.get_brightness(display=0)
            return int(value[0] if isinstance(value, list) else value)
        except Exception:
            return self.adjust_value

    def _adjust_brightness(self, delta):
        try:
            import screen_brightness_control as sbc

            current = self._get_brightness()
            target = max(0, min(100, current + delta))
            sbc.set_brightness(target, display=0)
            self.adjust_value = target

        except Exception as exc:
            print(
                f"Brightness control unavailable: {exc}",
                flush=True,
            )

    def _get_volume(self):
        try:
            volume = get_audio_endpoint_volume()
            return int(volume.GetMasterVolumeLevelScalar() * 100)
        except Exception as exc:
            print(f"Could not read system volume: {exc}", flush=True)
            return self.adjust_value

    def _adjust_volume(self, delta):
        try:
            volume = get_audio_endpoint_volume()
  
            current = volume.GetMasterVolumeLevelScalar() * 100
            target = max(0, min(100, current + delta))

            volume.SetMasterVolumeLevelScalar(target / 100, None)
            self.adjust_value = int(target)

        except Exception as exc:
            print(f"Volume control unavailable: {exc}", flush=True)

    def _adjust(self, direction):
        if self.page == "brightness":
            step = int(self.config.get("brightness_step", 5))
            self._adjust_brightness(direction * step)

        elif self.page == "volume":
            step = int(self.config.get("volume_step", 5))
            self._adjust_volume(direction * step)

        self.update()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Right, Qt.Key.Key_Down):
            self._rotate(1)
        elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            self._rotate(-1)
        elif event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self._select()
        elif event.key() == Qt.Key.Key_Escape:
            self.events.command.emit("escape")
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return

        center = QPointF(self.width() / 2, 218)
        point = event.position()
        dx = point.x() - center.x()
        dy = point.y() - center.y()

        if dx * dx + dy * dy < 55 * 55:
            self._select()
        else:
            options = self._current_options()
            if options:
                angle = (self._angle(point, center) + 90) % 360
                self.index = int(
                    angle / 360 * len(options)
                ) % len(options)
                self._select()

        self.update()

    @staticmethod
    def _angle(point, center):
        return math.degrees(
            math.atan2(
                point.y() - center.y(),
                point.x() - center.x(),
            )
        )

    def paintEvent(self, event):
        from PySide6.QtGui import QPainterPath, QLinearGradient

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        W, H = self.width(), self.height()
        cx, cy = W / 2, 218
        outer_r, inner_r = 163, 69

        blue = QColor("#078BFF")
        white = QColor("#F4F8FF")
        muted = QColor("#B8C9E2")

        # Glass-like rounded panel
        panel_gradient = QLinearGradient(0, 0, W, H)
        panel_gradient.setColorAt(0, QColor(7, 18, 39, 250))
        panel_gradient.setColorAt(0.5, QColor(5, 13, 29, 247))
        panel_gradient.setColorAt(1, QColor(9, 20, 39, 250))

        p.setPen(QPen(QColor(38, 108, 190, 150), 1))
        p.setBrush(panel_gradient)
        p.drawRoundedRect(4, 4, W - 8, H - 8, 28, 28)

        # Top branding
        p.setPen(white)
        p.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        p.drawText(
            20, 20, W - 40, 25,
            Qt.AlignmentFlag.AlignCenter,
            "CMF  SMART DIAL",
        )

        p.setPen(QColor("#7FADE3"))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(
            20, 43, W - 40, 18,
            Qt.AlignmentFlag.AlignCenter,
            "CONTROL CENTER",
        )

        # Circular outer border and subtle inner ring
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(0, 112, 255, 210), 1.5))
        p.drawEllipse(QPointF(cx, cy), outer_r + 5, outer_r + 5)

        p.setPen(QPen(QColor(20, 116, 255, 50), 1))
        p.drawEllipse(QPointF(cx, cy), outer_r - 3, outer_r - 3)

        # Menu item configuration
        options = self._current_options()

        if self.page == "home":
            definitions = [
                ("brightness", "☼", "#168BFF"),
                ("volume", "◖", "#12C8B7"),
                ("apps", "▦", "#7838F5"),
                ("links", "∞", "#E72D91"),
                ("media", "▶", "#F49A28"),
                ("settings", "⚙", "#609DFF"),
            ]
        else:
            definitions = [
                ("", "•", "#168BFF")
                for _ in options
            ]

        count = max(1, len(options))

        def point_at(radius, angle):
            rad = math.radians(angle)
            return QPointF(
                cx + radius * math.cos(rad),
                cy + radius * math.sin(rad),
            )

        def make_sector(start_angle, end_angle):
            path = QPainterPath()

            # Outer arc
            start = point_at(outer_r, start_angle)
            path.moveTo(start)

            steps = 18
            for j in range(1, steps + 1):
                angle = start_angle + (
                    end_angle - start_angle
                ) * j / steps
                path.lineTo(point_at(outer_r, angle))

            # Return along the inner arc in reverse
            for j in range(steps + 1):
                angle = end_angle - (
                    end_angle - start_angle
                ) * j / steps
                path.lineTo(point_at(inner_r, angle))

            path.closeSubpath()
            return path

        for i, (label, action) in enumerate(options):
            # Put the first item at the top, then continue clockwise
            center_angle = -90 + i * 360 / count
            start_angle = center_angle - (360 / count) / 2 + 3
            end_angle = center_angle + (360 / count) / 2 - 3

            selected = i == self.index

            if self.page == "home":
                key, glyph, color_hex = definitions[i]
            else:
                key, glyph, color_hex = "", "•", "#168BFF"

            accent = QColor(color_hex)
            sector = make_sector(start_angle, end_angle)

            # Selection glow
            if selected:
                p.setPen(QPen(
                    QColor(
                        accent.red(),
                        accent.green(),
                        accent.blue(),
                        100,
                    ),
                    5,
                ))
                p.setBrush(QColor(
                    accent.red(),
                    accent.green(),
                    accent.blue(),
                    30,
                ))
                p.drawPath(sector)

            # Translucent sector fill
            sector_gradient = QLinearGradient(
                0, cy - outer_r, 0, cy + outer_r
            )

            if selected:
                sector_gradient.setColorAt(
                    0, QColor(15, 81, 170, 220)
                )
                sector_gradient.setColorAt(
                    1, QColor(7, 29, 76, 245)
                )
            else:
                sector_gradient.setColorAt(
                    0, QColor(20, 39, 67, 225)
                )
                sector_gradient.setColorAt(
                    1, QColor(9, 20, 38, 245)
                )

            p.setBrush(sector_gradient)
            p.setPen(QPen(
                accent if selected else QColor(58, 83, 119, 190),
                2 if selected else 1,
            ))
            p.drawPath(sector)

            # Icon badge
            badge_radius = (inner_r + outer_r) / 2
            pos = point_at(badge_radius, center_angle)
            icon_y = pos.y() - 12

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(
                accent.red(),
                accent.green(),
                accent.blue(),
                85 if selected else 65,
            ))
            p.drawEllipse(QPointF(pos.x(), icon_y), 25, 25)

            p.setPen(white)
            p.setFont(QFont(
                "Segoe UI Symbol",
                25 if glyph != "⚙" else 22,
                QFont.Weight.DemiBold,
            ))
            p.drawText(
                int(pos.x() - 28),
                int(icon_y - 28),
                56, 56,
                Qt.AlignmentFlag.AlignCenter,
                glyph,
            )

            # Label
            label_y = icon_y + 44
            p.setPen(white if selected else muted)
            p.setFont(QFont(
                "Segoe UI",
                9,
                QFont.Weight.DemiBold if selected
                else QFont.Weight.Medium,
            ))
            p.drawText(
                int(pos.x() - 53),
                int(label_y),
                106, 22,
                Qt.AlignmentFlag.AlignCenter,
                label,
            )

        # Center hub
        p.setPen(QPen(QColor("#078BFF"), 2))
        p.setBrush(QColor(7, 20, 41, 255))
        p.drawEllipse(QPointF(cx, cy), inner_r - 2, inner_r - 2)

        p.setPen(QPen(QColor(0, 113, 255, 100), 5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), inner_r - 8, inner_r - 8)

        # Minimal dial emblem
        p.setPen(QPen(QColor("#168BFF"), 3))
        p.drawArc(
            int(cx - 15), int(cy - 51), 30, 30,
            35 * 16, 290 * 16,
        )

        p.setPen(QColor("#168BFF"))
        p.setBrush(QColor("#168BFF"))
        p.drawEllipse(QPointF(cx, cy - 36), 4, 4)

        # Brand text
        p.setPen(white)
        p.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        p.drawText(
            int(cx - 48), int(cy + 1), 96, 30,
            Qt.AlignmentFlag.AlignCenter,
            "cmf",
        )

        p.setPen(QColor("#63AFFF"))
        p.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
        p.drawText(
            int(cx - 48), int(cy + 27), 96, 18,
            Qt.AlignmentFlag.AlignCenter,
            "S M A R T   D I A L",
        )

        # Bottom instruction strip
        strip = self.rect().adjusted(16, H - 65, -16, -12)

        p.setPen(QPen(QColor(31, 103, 183, 210), 1))
        p.setBrush(QColor(8, 22, 42, 245))
        p.drawRoundedRect(strip, 20, 20)

        columns = [
            ("◉", "DOUBLE PRESS", "Open / Close"),
            ("↻", "ROTATE", "Navigate / Adjust"),
            ("○", "PRESS", "Select"),
        ]

        col_width = strip.width() / 3

        for i, (symbol, title, subtitle) in enumerate(columns):
            x = strip.x() + i * col_width

            if i:
                p.setPen(QPen(QColor(42, 78, 120), 1))
                p.drawLine(
                    int(x), strip.y() + 9,
                    int(x), strip.y() + strip.height() - 9,
                )

            p.setPen(QColor("#58A8FF"))
            p.setFont(QFont("Segoe UI Symbol", 17))
            p.drawText(
                int(x + 3), strip.y() + 12,
                34, 29,
                Qt.AlignmentFlag.AlignCenter,
                symbol,
            )

            p.setPen(white)
            p.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
            p.drawText(
                int(x + 34), strip.y() + 9,
                int(col_width - 35), 20,
                Qt.AlignmentFlag.AlignCenter,
                title,
            )

            p.setPen(QColor("#69B5FF"))
            p.setFont(QFont("Segoe UI", 7))
            p.drawText(
                int(x + 34), strip.y() + 29,
                int(col_width - 35), 18,
                Qt.AlignmentFlag.AlignCenter,
                subtitle,
            )

        p.end()


class Controller:
    def __init__(self, app, config):
        self.app = app
        self.config = config
        self.events = Events()
        self.window = RadialMenu(config, self.events)

        self.running = True
        self.last_play = 0.0
        self.press_lock = threading.Lock()
        self.last_single_press = 0.0
        self.keyboard_module = None
        self.keyboard_hook = None
        self.hotkey_handle = None

        self.audio_monitor = AudioVolumeMonitor(
            callback=self._on_rotation,
            get_page=lambda: self.window.page,
            get_window=lambda: self.window,
        )

        self._setup_tray()
        self.audio_monitor.start()

        threading.Thread(
            target=self._keyboard_worker,
            daemon=True,
            name="CMF-Keyboard-Listener",
        ).start()

    def _setup_tray(self):
        self.tray = None

        if not QSystemTrayIcon.isSystemTrayAvailable():
            print("[TRAY] System tray is unavailable.", flush=True)
            return

        self.tray = QSystemTrayIcon(self.app)
        self.tray.setToolTip("CMF Smart Dial Controller")

        menu = QMenu()
        menu.addAction(
            "Open radial menu",
            self.window.show_menu,
        )
        menu.addAction(
            "Edit config.json",
            self._open_config,
        )
        menu.addAction(
            "Quit",
            self.quit,
        )

        self.tray.setContextMenu(menu)
        self.tray.show()

    @staticmethod
    def _open_config():
        try:
            os.startfile(str(CONFIG_PATH))
        except Exception as exc:
            print(f"Could not open config.json: {exc}", flush=True)

    def _on_rotation(self, direction):
        command = "rotate_right" if direction > 0 else "rotate_left"
        self.events.command.emit(command)

    def _keyboard_worker(self):
        try:
            import keyboard
            self.keyboard_module = keyboard

            print("[KEYBOARD] Listening for media-key events.", flush=True)
            print("[KEYBOARD] Double play/pause toggles the menu.", flush=True)
            print("[KEYBOARD] Single play/pause selects an item.", flush=True)
            print("[KEYBOARD] Next/previous track navigate the menu.", flush=True)
            print("[KEYBOARD] F9 toggles the menu.", flush=True)

            self.hotkey_handle = keyboard.add_hotkey(
                "f9",
                lambda: self.events.command.emit("toggle_menu"),
            )

            self.keyboard_hook = keyboard.hook(self._on_key)

            while self.running:
                time.sleep(0.2)

        except Exception as exc:
            print(
                "[KEYBOARD ERROR] Keyboard listener unavailable: "
                f"{exc}",
                flush=True,
            )

        finally:
            if self.keyboard_module is not None:
                try:
                    self.keyboard_module.unhook_all()
                except Exception:
                    pass

    

    def _on_key(self, event):
        if event.event_type != "down":
            return

        scan_code = event.scan_code

        if scan_code == -176:
            if not self.window.isVisible():
                print("[DIAL] Opening menu", flush=True)
                self.events.command.emit("toggle_menu")

        elif scan_code == -177:
            if self.window.isVisible():
                print("[DIAL] Closing menu", flush=True)
                self.events.command.emit("close_menu")

        elif scan_code == -179:
            if self.window.isVisible():
                print("[DIAL] Selecting menu option", flush=True)
                self.events.command.emit("select")
  

    def quit(self):
        self.running = False
        self.audio_monitor.stop()

        try:
            if self.tray is not None:
                self.tray.hide()
        except Exception:
            pass

        self.app.quit()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    config = load_config()
    controller = Controller(app, config)

    print("[READY] CMF Smart Dial Controller started.", flush=True)
    print("[READY] Opening the radial menu.", flush=True)

   

    # Keep a Python reference to the controller while Qt is running.
    app.controller = controller

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
