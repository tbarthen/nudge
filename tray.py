import ctypes
import ctypes.wintypes
import json
import os
import threading
import time

import urllib.request

import pystray
from PIL import Image, ImageDraw

import popup
from data import DATA_FILE

ICON_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")


def _get_config():
    """Read config from reminders.json."""
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f).get("config", {})
    except Exception:
        return {"popup_interval_minutes": 60, "server_port": 5123}


def _get_port():
    """Get the server port from config."""
    return _get_config().get("server_port", 5123)


def _get_interval():
    """Get the popup interval in minutes from config, minimum 1."""
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

    # Notepad body
    pad_left, pad_top, pad_right, pad_bottom = 2, 2, 52, 62
    draw.rounded_rectangle([pad_left, pad_top, pad_right, pad_bottom], radius=5, fill="#f5f0e8", outline="#bbb5a5", width=2)

    # Spiral binding along top
    for cx in range(10, 50, 8):
        draw.ellipse([cx - 2, pad_top - 2, cx + 2, pad_top + 2], fill="#999")

    # Ruled lines
    for ly in range(16, 58, 8):
        draw.line([(pad_left + 5, ly), (pad_right - 5, ly)], fill="#c5c0b5", width=1)

    # Green checkmark circle (bottom-right)
    check_cx, check_cy, check_r = 46, 46, 18
    draw.ellipse([check_cx - check_r, check_cy - check_r, check_cx + check_r, check_cy + check_r],
                 fill="#2ecc71", outline="#1fa85a", width=2)

    # Checkmark stroke
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

        slept = 0
        while slept < interval_min * 60 and not stop_event.is_set():
            time.sleep(5)
            slept += 5

        if stop_event.is_set():
            break

        idle_sec = _get_idle_seconds()
        interval_sec = _get_interval() * 60
        if idle_sec > interval_sec:
            while _get_idle_seconds() > 30 and not stop_event.is_set():
                time.sleep(5)
            if stop_event.is_set():
                break

        port = _get_port()
        if _has_active_reminders(port):
            if not popup.popup_visible:
                popup.show_popup(port)


def _pair_phone(port):
    """Generate a pairing QR code and show it in a dialog."""
    import tkinter as tk
    import qrcode
    import io

    def do_pair():
        try:
            url = f"http://localhost:{port}/api/pair/generate"
            req = urllib.request.Request(url, method="POST", data=b"")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=3) as resp:
                result = json.loads(resp.read().decode())
                code = result.get("code", "???")
                local_ip = result.get("ip", "?.?.?.?")
        except Exception:
            code = "Error"
            local_ip = "?"

        pair_url = f"https://tbarthen.github.io/nudge/?server={local_ip}:{port}&code={code}"

        qr = qrcode.QRCode(box_size=6, border=2)
        qr.add_data(pair_url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="#e0e0e0", back_color=popup.BG).convert("RGBA")

        win = tk.Toplevel(popup._root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=popup.BG)

        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        qr_size = qr_img.size[0]
        w = max(qr_size + 40, 300)
        h = qr_size + 120
        win.geometry(f"{w}x{h}+{(screen_w - w) // 2}+{(screen_h - h) // 2}")

        tk.Label(win, text="Scan to pair phone", font=("Segoe UI", 14, "bold"),
                 bg=popup.BG, fg=popup.ACCENT).pack(pady=(16, 8))

        from PIL import ImageTk
        tk_img = ImageTk.PhotoImage(qr_img)
        qr_label = tk.Label(win, image=tk_img, bg=popup.BG)
        qr_label.image = tk_img  # prevent GC
        qr_label.pack(pady=(0, 4))

        tk.Label(win, text="Expires in 5 minutes",
                 font=("Segoe UI", 8), bg=popup.BG, fg="#5a5a7a").pack(pady=(2, 8))

        tk.Button(win, text="Done", font=("Segoe UI", 10),
                  bg=popup.CARD_BG, fg=popup.FG, bd=0, padx=12, cursor="hand2",
                  command=win.destroy).pack()

        win.bind("<Escape>", lambda e: win.destroy())

    if popup._root:
        popup._root.after(0, do_pair)


def _share_app(port):
    """Show a share dialog with the GitHub repo link."""
    import tkinter as tk

    def do_share():
        win = tk.Toplevel(popup._root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=popup.BG)

        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        w, h = 360, 180
        win.geometry(f"{w}x{h}+{(screen_w - w) // 2}+{(screen_h - h) // 2}")

        tk.Label(win, text="Share Nudge", font=("Segoe UI", 14, "bold"),
                 bg=popup.BG, fg=popup.ACCENT).pack(pady=(16, 4))
        tk.Label(win, text="Give someone their own copy of Nudge",
                 font=("Segoe UI", 9), bg=popup.BG, fg="#8a8a9a").pack(pady=(0, 12))

        url = "https://tbarthen.github.io/nudge/"
        url_var = tk.StringVar(value=url)
        entry = tk.Entry(win, textvariable=url_var, font=("Consolas", 10),
                         bg=popup.CARD_BG, fg=popup.FG, bd=0, readonlybackground=popup.CARD_BG,
                         state="readonly", justify="center")
        entry.pack(fill="x", padx=24, ipady=4)

        btn_frame = tk.Frame(win, bg=popup.BG)
        btn_frame.pack(pady=12)

        def copy_link():
            win.clipboard_clear()
            win.clipboard_append(url)
            copy_btn.configure(text="Copied!")
            win.after(1500, lambda: copy_btn.configure(text="Copy link"))

        copy_btn = tk.Button(btn_frame, text="Copy link", font=("Segoe UI", 10),
                             bg=popup.ACCENT, fg="white", bd=0, padx=12, cursor="hand2",
                             command=copy_link)
        copy_btn.pack(side="left", padx=4)

        tk.Button(btn_frame, text="Done", font=("Segoe UI", 10),
                  bg=popup.CARD_BG, fg=popup.FG, bd=0, padx=12, cursor="hand2",
                  command=win.destroy).pack(side="left", padx=4)

        win.bind("<Escape>", lambda e: win.destroy())

    if popup._root:
        popup._root.after(0, do_share)


def _show_settings(port):
    """Show a settings dialog."""
    import tkinter as tk

    def do_settings():
        config = _get_config()
        win = tk.Toplevel(popup._root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=popup.BG)

        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        w, h = 340, 340
        win.geometry(f"{w}x{h}+{(screen_w - w) // 2}+{(screen_h - h) // 2}")

        title_frame = tk.Frame(win, bg=popup.ACCENT, height=36)
        title_frame.pack(fill="x")
        title_frame.pack_propagate(False)
        tk.Label(title_frame, text="Settings", font=("Segoe UI", 12, "bold"),
                 bg=popup.ACCENT, fg="white").pack(side="left", padx=12, pady=6)
        tk.Button(title_frame, text="\u2715", font=("Segoe UI", 10), bg=popup.ACCENT, fg="white",
                  bd=0, activebackground="#c0392b", cursor="hand2",
                  command=win.destroy).pack(side="right", padx=8)

        def start_drag(e):
            win._drag_x, win._drag_y = e.x, e.y
        def do_drag(e):
            win.geometry(f"+{win.winfo_x() + e.x - win._drag_x}+{win.winfo_y() + e.y - win._drag_y}")
        title_frame.bind("<Button-1>", start_drag)
        title_frame.bind("<B1-Motion>", do_drag)

        body = tk.Frame(win, bg=popup.BG)
        body.pack(fill="both", expand=True, padx=16, pady=12)

        row_font = ("Segoe UI", 10)
        label_fg = "#8a8aaa"

        tk.Label(body, text="Popup interval", font=row_font, bg=popup.BG, fg=label_fg,
                 anchor="w").pack(fill="x", pady=(0, 2))
        interval_frame = tk.Frame(body, bg=popup.BG)
        interval_frame.pack(fill="x", pady=(0, 12))
        interval_var = tk.StringVar(value=str(config.get("popup_interval_minutes", 60)))
        interval_options = [("1 min (test)", "1"), ("15 min", "15"), ("30 min", "30"),
                            ("45 min", "45"), ("60 min", "60"), ("90 min", "90"), ("2 hours", "120")]
        for text, val in interval_options:
            tk.Radiobutton(interval_frame, text=text, variable=interval_var, value=val,
                           font=("Segoe UI", 9), bg=popup.BG, fg=popup.FG,
                           selectcolor=popup.CARD_BG, activebackground=popup.BG,
                           activeforeground=popup.FG, anchor="w",
                           highlightthickness=0, bd=0).pack(anchor="w")

        tk.Label(body, text="List auto-refresh", font=row_font, bg=popup.BG, fg=label_fg,
                 anchor="w").pack(fill="x", pady=(0, 2))
        refresh_frame = tk.Frame(body, bg=popup.BG)
        refresh_frame.pack(fill="x", pady=(0, 12))
        refresh_entry = tk.Entry(refresh_frame, font=row_font, bg=popup.CARD_BG, fg=popup.FG,
                                 insertbackground=popup.FG, bd=0, width=5)
        refresh_entry.insert(0, str(config.get("auto_refresh_seconds", 90)))
        refresh_entry.pack(side="left", ipady=2)
        tk.Label(refresh_frame, text="seconds", font=("Segoe UI", 9), bg=popup.BG, fg=label_fg).pack(side="left", padx=6)

        tk.Label(body, text="Keep completed items for", font=row_font, bg=popup.BG, fg=label_fg,
                 anchor="w").pack(fill="x", pady=(0, 2))
        ret_frame = tk.Frame(body, bg=popup.BG)
        ret_frame.pack(fill="x", pady=(0, 12))
        ret_entry = tk.Entry(ret_frame, font=row_font, bg=popup.CARD_BG, fg=popup.FG,
                             insertbackground=popup.FG, bd=0, width=5)
        ret_entry.insert(0, str(config.get("completed_retention_days", 60)))
        ret_entry.pack(side="left", ipady=2)
        tk.Label(ret_frame, text="days", font=("Segoe UI", 9), bg=popup.BG, fg=label_fg).pack(side="left", padx=6)

        start_var = tk.BooleanVar(value=config.get("start_with_windows", False))
        tk.Checkbutton(body, text="Start with Windows", variable=start_var,
                       font=row_font, bg=popup.BG, fg=popup.FG,
                       selectcolor=popup.CARD_BG, activebackground=popup.BG,
                       activeforeground=popup.FG, highlightthickness=0, bd=0).pack(anchor="w", pady=(0, 12))

        def save():
            updates = {}
            try:
                iv = int(interval_var.get())
                if iv >= 1:
                    updates["popup_interval_minutes"] = iv
            except ValueError:
                pass
            try:
                rs = int(refresh_entry.get())
                if rs >= 10:
                    updates["auto_refresh_seconds"] = rs
            except ValueError:
                pass
            try:
                rd = int(ret_entry.get())
                if rd >= 1:
                    updates["completed_retention_days"] = rd
            except ValueError:
                pass
            updates["start_with_windows"] = start_var.get()

            if updates:
                def bg():
                    try:
                        url = f"http://localhost:{port}/api/config"
                        data = json.dumps(updates).encode()
                        req = urllib.request.Request(url, method="PUT", data=data)
                        req.add_header("Content-Type", "application/json")
                        urllib.request.urlopen(req, timeout=2)
                    except Exception:
                        pass
                threading.Thread(target=bg, daemon=True).start()
            win.destroy()

        tk.Button(body, text="Save", font=("Segoe UI", 10, "bold"), bg=popup.ACCENT, fg="white",
                  bd=0, padx=16, cursor="hand2", command=save).pack(side="left")
        tk.Button(body, text="Cancel", font=("Segoe UI", 10), bg=popup.CARD_BG, fg=popup.FG,
                  bd=0, padx=12, cursor="hand2", command=win.destroy).pack(side="left", padx=8)

        win.bind("<Escape>", lambda e: win.destroy())

    if popup._root:
        popup._root.after(0, do_settings)


def run_tray(stop_event=None):
    """Run the system tray icon. Blocks on main thread or calling thread."""
    if stop_event is None:
        stop_event = threading.Event()

    generate_icon()

    def on_show_reminders(icon, item):
        port = _get_port()
        popup.show_popup(port)

    def on_pair(icon, item):
        port = _get_port()
        _pair_phone(port)

    def on_share(icon, item):
        port = _get_port()
        _share_app(port)

    def on_settings(icon, item):
        port = _get_port()
        _show_settings(port)

    def on_quit(icon, item):
        stop_event.set()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Show Reminders", on_show_reminders, default=True, visible=False),
        pystray.MenuItem("Pair Phone", on_pair),
        pystray.MenuItem("Share", on_share),
        pystray.MenuItem("Settings", on_settings),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )

    icon = pystray.Icon("nudge", _create_image(), "Nudge — DO IT!!!", menu)

    timer_thread = threading.Thread(target=_timer_loop, args=(stop_event,), daemon=True)
    timer_thread.start()

    icon.run()
