import ctypes
import ctypes.wintypes
import json
import os
import threading
import time
import webbrowser
import urllib.request

import pystray
from PIL import Image, ImageDraw

import popup

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders.json")
ICON_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")


def _get_config():
    """Read config from reminders.json."""
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f).get("config", {})
    except Exception:
        return {"popup_interval_minutes": 60, "server_port": 5123}


def _get_port():
    return _get_config().get("server_port", 5123)


def _get_interval():
    val = _get_config().get("popup_interval_minutes", 60)
    return max(1, int(val)) if isinstance(val, (int, float)) else 60


def _has_active_reminders(port):
    """Check if there are any active reminders."""
    try:
        url = f"http://localhost:{port}/api/reminders"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as resp:
            reminders = json.loads(resp.read().decode())
            return len(reminders) > 0
    except Exception:
        return False


def _get_idle_seconds():
    """Get system idle time in seconds via Win32 API."""
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("dwTime", ctypes.c_uint),
            ]
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
        millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
        return millis / 1000.0
    except Exception:
        return 0


def generate_icon(force=False):
    """Generate a tray icon with 'DO!' in a circle."""
    if not force and os.path.exists(ICON_FILE):
        return

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Notepad body — fills most of the canvas, leaving room for checkmark overlap
    pad_left, pad_top, pad_right, pad_bottom = 2, 2, 52, 62
    draw.rounded_rectangle([pad_left, pad_top, pad_right, pad_bottom], radius=5, fill="#f5f0e8", outline="#bbb5a5", width=2)

    # Notepad top spiral binding — small circles along the top
    for cx in range(10, 50, 8):
        draw.ellipse([cx - 2, pad_top - 2, cx + 2, pad_top + 2], fill="#999")

    # Ruled lines on the notepad
    for ly in range(16, 58, 8):
        draw.line([(pad_left + 5, ly), (pad_right - 5, ly)], fill="#c5c0b5", width=1)

    # Green checkmark circle in bottom-right — bigger and bolder
    check_cx, check_cy, check_r = 46, 46, 18
    draw.ellipse([check_cx - check_r, check_cy - check_r, check_cx + check_r, check_cy + check_r],
                 fill="#2ecc71", outline="#1fa85a", width=2)

    # Checkmark stroke — thicker white lines
    draw.line([(36, 46), (44, 54)], fill="white", width=4)
    draw.line([(44, 54), (56, 38)], fill="white", width=4)

    img.save(ICON_FILE, format="ICO")


def _create_image():
    """Load the tray icon image."""
    generate_icon()
    return Image.open(ICON_FILE)


def _timer_loop(stop_event):
    """Timer thread that fires popups at the configured interval."""
    while not stop_event.is_set():
        interval_min = _get_interval()
        port = _get_port()

        # Sleep in small increments so we can respond to stop_event and config changes
        slept = 0
        while slept < interval_min * 60 and not stop_event.is_set():
            time.sleep(5)
            slept += 5

        if stop_event.is_set():
            break

        # Check idle — if idle longer than interval, skip (will fire on next active check)
        idle_sec = _get_idle_seconds()
        interval_sec = _get_interval() * 60
        if idle_sec > interval_sec:
            # User is away — wait for them to come back
            while _get_idle_seconds() > 30 and not stop_event.is_set():
                time.sleep(5)
            if stop_event.is_set():
                break

        # Only show popup if there are active reminders
        port = _get_port()
        if _has_active_reminders(port):
            if not popup.popup_visible:
                popup.show_popup(port)


def run_tray(stop_event=None):
    """Run the system tray icon. Blocks on main thread or calling thread."""
    if stop_event is None:
        stop_event = threading.Event()

    generate_icon()

    def on_open_web(icon, item):
        port = _get_port()
        webbrowser.open(f"http://localhost:{port}")

    def on_show_reminders(icon, item):
        port = _get_port()
        popup.show_popup(port)

    def on_quit(icon, item):
        stop_event.set()
        icon.stop()

    def on_left_click(icon):
        port = _get_port()
        webbrowser.open(f"http://localhost:{port}")

    menu = pystray.Menu(
        pystray.MenuItem("Show Reminders", on_show_reminders, default=True),
        pystray.MenuItem("Open Web UI", on_open_web),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )

    icon = pystray.Icon("nudge", _create_image(), "Nudge — DO IT!!!", menu)

    # Start the timer thread
    timer_thread = threading.Thread(target=_timer_loop, args=(stop_event,), daemon=True)
    timer_thread.start()

    # pystray needs to run on the thread that calls it (preferably main)
    icon.run()
