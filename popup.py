import tkinter as tk
import tkinter.font as tkfont
import ctypes
import threading
import urllib.request
import json

# Flag shared with tray.py
popup_visible = False
_popup_lock = threading.Lock()
_root = None
_initialized = threading.Event()
_port = 5123
_cached_reminders = []
_auto_hide_id = None

# Colors
BG = "#1a1a2e"
FG = "#e0e0e0"
ACCENT = "#e94560"
CARD_BG = "#16213e"
BTN_BG = "#0f3460"

WIN_W = 555
WIN_H_DEFAULT_RATIO = 0.55  # Default to 55% of screen height
WIN_H_MAX_RATIO = 0.75  # Max 75% of screen height
CHROME_H = 118  # title bar + add bar + padding
ROW_H = 46  # approximate height per reminder row


def _get_taskbar_height():
    try:
        from ctypes import wintypes
        class APPBARDATA(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("hWnd", ctypes.POINTER(ctypes.c_int)),
                ("uCallbackMessage", ctypes.c_uint),
                ("uEdge", ctypes.c_uint),
                ("rc", wintypes.RECT),
                ("lParam", ctypes.c_int),
            ]
        abd = APPBARDATA()
        abd.cbSize = ctypes.sizeof(APPBARDATA)
        ctypes.windll.shell32.SHAppBarMessage(5, ctypes.byref(abd))
        screen_h = ctypes.windll.user32.GetSystemMetrics(1)
        taskbar_h = screen_h - abd.rc.top
        if 0 < taskbar_h < screen_h // 2:
            return taskbar_h
    except Exception:
        pass
    return 48


def _fetch_reminders(port):
    try:
        url = f"http://localhost:{port}/api/reminders"
        with urllib.request.urlopen(url, timeout=2) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return []


def _api_call(port, path, method="PATCH"):
    try:
        url = f"http://localhost:{port}{path}"
        req = urllib.request.Request(url, method=method, data=b"")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _api_put(port, path, body):
    try:
        url = f"http://localhost:{port}{path}"
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, method="PUT", data=data)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _api_post(port, path, body):
    try:
        url = f"http://localhost:{port}{path}"
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, method="POST", data=data)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _api_delete(port, path):
    try:
        url = f"http://localhost:{port}{path}"
        req = urllib.request.Request(url, method="DELETE")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _show_undo(action, item_id, item_text):
    """Show an undo bar at the bottom of the popup."""
    root = _root
    if not root:
        return
    undo_frame = root._undo_frame

    # Cancel any previous undo timer
    if undo_frame._undo_timer:
        root.after_cancel(undo_frame._undo_timer)
        undo_frame._undo_timer = None

    # Clear previous contents
    for w in undo_frame.winfo_children():
        w.destroy()

    label_text = "Completed" if action == "complete" else "Deleted"
    display = item_text if len(item_text) <= 30 else item_text[:27] + "..."

    tk.Label(undo_frame, text=f"{label_text}: {display}", font=("Segoe UI", 9),
             bg="#2a2a4a", fg="#ccc", anchor="w").pack(side="left", padx=(10, 4), pady=6)

    def do_undo():
        _dismiss_undo()
        port = _port
        def bg():
            if action == "complete":
                _api_call(port, f"/api/completed/{item_id}/uncomplete", "PATCH")
            elif action == "delete":
                # Re-create the reminder via POST
                _api_post(port, "/api/reminders", {"text": item_text, "id": item_id})
            root.after(0, _rebuild_list)
        threading.Thread(target=bg, daemon=True).start()

    tk.Button(undo_frame, text="Undo", font=("Segoe UI", 9, "bold"),
              bg=ACCENT, fg="white", bd=0, padx=10, cursor="hand2",
              command=do_undo).pack(side="right", padx=(4, 10), pady=6)

    try:
        undo_frame.pack(side="bottom", fill="x", before=root._canvas)
    except tk.TclError:
        undo_frame.pack(side="bottom", fill="x")

    # Stays visible until user clicks Undo or completes/deletes another item


def _dismiss_undo():
    """Hide the undo bar."""
    root = _root
    if not root:
        return
    undo_frame = root._undo_frame
    if undo_frame._undo_timer:
        root.after_cancel(undo_frame._undo_timer)
        undo_frame._undo_timer = None
    undo_frame.pack_forget()


def _init_window():
    """Create the persistent Tk window (hidden). Called once on a dedicated thread."""
    global _root, _initialized, popup_visible

    root = tk.Tk()
    _root = root
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.95)
    root.configure(bg=BG)
    root.withdraw()  # Start hidden

    # Position — start with default height, will resize after content loads
    taskbar_h = _get_taskbar_height()
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    default_h = int(screen_h * WIN_H_DEFAULT_RATIO)
    x = screen_w - WIN_W - 16
    y = screen_h - default_h - taskbar_h - 8
    root.geometry(f"{WIN_W}x{default_h}+{x}+{y}")

    # Title bar
    title_frame = tk.Frame(root, bg=ACCENT, height=44)
    title_frame.pack(fill="x")
    title_frame.pack_propagate(False)

    title_font = tkfont.Font(root=root, family="Segoe UI", size=16, weight="bold")
    tk.Label(title_frame, text="DO IT!!!", font=title_font, bg=ACCENT, fg="white").pack(side="left", padx=12, pady=6)

    close_btn = tk.Button(title_frame, text="\u2715", font=("Segoe UI", 12), bg=ACCENT, fg="white",
                          bd=0, activebackground="#c0392b", activeforeground="white",
                          cursor="hand2", command=_hide_popup)
    close_btn.pack(side="right", padx=8, pady=6)

    # Draggable title bar
    def start_drag(e):
        root._drag_x = e.x
        root._drag_y = e.y

    def do_drag(e):
        nx = root.winfo_x() + e.x - root._drag_x
        ny = root.winfo_y() + e.y - root._drag_y
        root.geometry(f"+{nx}+{ny}")

    title_frame.bind("<Button-1>", start_drag)
    title_frame.bind("<B1-Motion>", do_drag)

    # Add reminder input bar
    add_frame = tk.Frame(root, bg=BG, pady=6, padx=8)
    add_frame.pack(fill="x")

    font_placeholder = tkfont.Font(root=root, family="Segoe UI", size=10, slant="italic")
    font_entry = tkfont.Font(root=root, family="Segoe UI", size=10)
    placeholder_fg = "#5a5a7a"

    add_entry = tk.Entry(add_frame, font=font_placeholder, bg=CARD_BG, fg=placeholder_fg,
                         insertbackground=FG, bd=0, relief="flat")
    add_entry.insert(0, "What needs doing?")
    root._add_entry = add_entry

    def on_entry_focus_in(e):
        if add_entry.get() == "What needs doing?":
            add_entry.delete(0, "end")
            add_entry.configure(fg=FG, font=font_entry)

    def on_entry_focus_out(e):
        if not add_entry.get().strip():
            add_entry.insert(0, "What needs doing?")
            add_entry.configure(fg=placeholder_fg, font=font_placeholder)

    def on_add_submit(e=None):
        text = add_entry.get().strip()
        if not text or text == "What needs doing?":
            return
        add_entry.delete(0, "end")
        add_entry.configure(fg=FG, font=font_entry)
        port = _port
        def bg():
            try:
                url = f"http://localhost:{port}/api/reminders"
                data = json.dumps({"text": text}).encode()
                req = urllib.request.Request(url, method="POST", data=data)
                req.add_header("Content-Type", "application/json")
                urllib.request.urlopen(req, timeout=2)
            except Exception:
                pass
            root.after(0, _rebuild_list)
        threading.Thread(target=bg, daemon=True).start()

    add_entry.bind("<FocusIn>", on_entry_focus_in)
    add_entry.bind("<FocusOut>", on_entry_focus_out)
    add_entry.bind("<Return>", on_add_submit)
    add_entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 6))

    add_btn = tk.Button(add_frame, text="+", font=("Segoe UI", 12, "bold"), bg=ACCENT, fg="white",
                        bd=0, width=3, cursor="hand2", command=on_add_submit)
    add_btn.pack(side="right")

    # Refresh indicator (thin bar below add input, above list)
    refresh_label = tk.Label(root, text="\u2022 syncing...", font=("Segoe UI", 8),
                             bg=BG, fg="#555577", anchor="w")
    root._refresh_label = refresh_label
    # Not packed yet — shown/hidden dynamically

    # Scrollable content area
    canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
    scrollbar = tk.Scrollbar(root, orient="vertical", command=canvas.yview)
    content_frame = tk.Frame(canvas, bg=BG)

    def _on_content_configure(e):
        canvas.configure(scrollregion=canvas.bbox("all"))
        # Show scrollbar only when content exceeds visible area
        content_height = content_frame.winfo_reqheight()
        canvas_height = canvas.winfo_height()
        if content_height > canvas_height and canvas_height > 1:
            if not scrollbar.winfo_ismapped():
                scrollbar.pack(side="right", fill="y")
        else:
            if scrollbar.winfo_ismapped():
                scrollbar.pack_forget()

    content_frame.bind("<Configure>", _on_content_configure)
    canvas.bind("<Configure>", lambda e: _on_content_configure(None))
    canvas.create_window((0, 0), window=content_frame, anchor="nw", width=WIN_W - 16)
    canvas.configure(yscrollcommand=scrollbar.set)

    # Don't pack scrollbar by default — it will show dynamically when needed
    canvas.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)

    # Undo bar (hidden by default, shown after complete/delete)
    undo_frame = tk.Frame(root, bg="#2a2a4a", height=36)
    undo_frame._undo_timer = None
    root._undo_frame = undo_frame
    # Not packed — shown dynamically

    def on_mousewheel(e):
        if content_frame.winfo_reqheight() > canvas.winfo_height():
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
    canvas.bind_all("<MouseWheel>", on_mousewheel)

    # Cache fonts so we don't leak Font objects on every rebuild
    root._font_done = tkfont.Font(root=root, family="Segoe UI", size=18, weight="bold")
    root._font_item = tkfont.Font(root=root, family="Segoe UI", size=11)
    root._font_btn = tkfont.Font(root=root, family="Segoe UI", size=10)
    root._font_handle = tkfont.Font(root=root, family="Segoe UI", size=11)

    # Drag-and-drop state
    root._drag_item = None       # index of item being dragged
    root._drag_float = None      # floating label widget
    root._drag_rows = []         # list of row frames (in display order)
    root._drag_reminders = []    # reminders list for reorder
    root._drag_target = None     # index of current drop target
    root._drag_indicator = None  # visual drop indicator line

    # Store references for rebuild
    root._content_frame = content_frame
    root._canvas = canvas

    root.protocol("WM_DELETE_WINDOW", _hide_popup)

    _initialized.set()
    root.mainloop()


def _resize_to_fit(num_items):
    """Resize the popup window to fit the number of items, anchored to bottom-right."""
    root = _root
    if not root:
        return

    taskbar_h = _get_taskbar_height()
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    default_h = int(screen_h * WIN_H_DEFAULT_RATIO)
    max_h = int(screen_h * WIN_H_MAX_RATIO)

    if num_items == 0:
        content_h = CHROME_H + 100  # "You DID it!!!" message
    else:
        content_h = CHROME_H + num_items * ROW_H

    # Use whichever is larger: the content size or the default size
    win_h = min(max(content_h, default_h), max_h)
    x = screen_w - WIN_W - 16
    y = screen_h - win_h - taskbar_h - 8
    root.geometry(f"{WIN_W}x{win_h}+{x}+{y}")


def _hide_popup():
    global popup_visible, _auto_refresh_id
    with _popup_lock:
        popup_visible = False
    if _root:
        if _auto_refresh_id is not None:
            _root.after_cancel(_auto_refresh_id)
            _auto_refresh_id = None
        _root.withdraw()


def _rebuild_list():
    """Rebuild the reminder list in the popup. Must be called from the Tk thread."""
    global _cached_reminders
    root = _root
    if not root:
        return

    port = _port

    # Show cached data immediately (no flash)
    if _cached_reminders:
        _populate_list(_cached_reminders, is_cache=True)

    # Show sync indicator
    try:
        root._refresh_label.pack(fill="x", padx=12, before=root._canvas)
    except tk.TclError:
        root._refresh_label.pack(fill="x", padx=12)

    def do_fetch():
        reminders = _fetch_reminders(port)
        root.after(0, lambda: _on_fresh_data(reminders))

    threading.Thread(target=do_fetch, daemon=True).start()


def _on_fresh_data(reminders):
    """Called when fresh data arrives from the API."""
    global _cached_reminders
    root = _root
    if not root:
        return

    # Hide sync indicator
    root._refresh_label.pack_forget()

    # Only re-render if data actually changed
    if reminders != _cached_reminders:
        _cached_reminders = reminders
        _populate_list(reminders, is_cache=False)
    else:
        _cached_reminders = reminders


def _drag_start(event, idx):
    """Begin dragging a reminder row."""
    root = _root
    if not root or idx < 0 or idx >= len(root._drag_rows):
        return
    root._drag_item = idx
    row = root._drag_rows[idx]
    reminder = root._drag_reminders[idx]

    # Create floating label that follows the cursor
    text = reminder["text"]
    if len(text) > 50:
        text = text[:47] + "..."
    float_label = tk.Label(root, text=text, font=root._font_item,
                           bg=ACCENT, fg="white", padx=8, pady=4, relief="solid", bd=1)
    float_label.place(x=event.x_root - root.winfo_rootx(),
                      y=event.y_root - root.winfo_rooty() - 15)
    root._drag_float = float_label

    # Dim the source row
    row.configure(bg="#0d1a30")
    for child in row.winfo_children():
        try:
            child.configure(bg="#0d1a30")
        except tk.TclError:
            pass


def _drag_motion(event):
    """Move the floating label and highlight drop target."""
    root = _root
    if root._drag_item is None or not root._drag_float:
        return

    # Move floating label
    rx = event.x_root - root.winfo_rootx()
    ry = event.y_root - root.winfo_rooty() - 15
    root._drag_float.place(x=rx, y=ry)

    # Determine which row we're over
    canvas = root._canvas
    content_frame = root._content_frame
    rows = root._drag_rows

    # Clean up previous indicator
    if root._drag_indicator:
        root._drag_indicator.place_forget()
        root._drag_indicator = None

    target_idx = None
    for i, row in enumerate(rows):
        try:
            ry_row = row.winfo_rooty()
            rh = row.winfo_height()
            if event.y_root >= ry_row and event.y_root < ry_row + rh:
                # Decide if inserting above or below midpoint
                mid = ry_row + rh // 2
                if event.y_root < mid:
                    target_idx = i
                else:
                    target_idx = i + 1
                break
        except tk.TclError:
            continue

    # If cursor is below the last row, target the end
    if target_idx is None and rows:
        try:
            last_row = rows[-1]
            last_bottom = last_row.winfo_rooty() + last_row.winfo_height()
            if event.y_root >= last_bottom:
                target_idx = len(rows)
        except tk.TclError:
            pass

    if target_idx is not None and target_idx != root._drag_item and target_idx != root._drag_item + 1:
        root._drag_target = target_idx
        # Show a colored line at the insertion point
        if root._drag_indicator is None:
            root._drag_indicator = tk.Frame(root, bg=ACCENT, height=3)
        # Position the indicator
        if target_idx < len(rows):
            ref_row = rows[target_idx]
            ind_y = ref_row.winfo_rooty() - root.winfo_rooty() - 2
        else:
            ref_row = rows[-1]
            ind_y = ref_row.winfo_rooty() + ref_row.winfo_height() - root.winfo_rooty() + 1
        root._drag_indicator.place(x=8, y=ind_y, width=WIN_W - 32, height=3)
    else:
        root._drag_target = None


def _drag_end(event):
    """Drop the dragged item at the target position."""
    root = _root
    if not root:
        return

    drag_idx = root._drag_item
    target_idx = root._drag_target

    # Clean up visuals
    if root._drag_float:
        root._drag_float.destroy()
        root._drag_float = None
    if root._drag_indicator:
        root._drag_indicator.place_forget()
        root._drag_indicator = None

    # Restore source row color
    if drag_idx is not None and drag_idx < len(root._drag_rows):
        row = root._drag_rows[drag_idx]
        try:
            row.configure(bg=CARD_BG)
            for child in row.winfo_children():
                try:
                    child.configure(bg=CARD_BG)
                except tk.TclError:
                    pass
        except tk.TclError:
            pass

    if drag_idx is None or target_idx is None:
        root._drag_item = None
        root._drag_target = None
        return

    reminders = root._drag_reminders
    if drag_idx == target_idx or drag_idx + 1 == target_idx:
        root._drag_item = None
        root._drag_target = None
        return

    # Build new order: remove dragged item, insert at target
    ordered = list(reminders)
    item = ordered.pop(drag_idx)
    if target_idx > drag_idx:
        target_idx -= 1
    ordered.insert(target_idx, item)

    # Assign new order values (highest = first displayed)
    reorder_payload = []
    for i, r in enumerate(ordered):
        new_order = len(ordered) - 1 - i
        r["order"] = new_order
        reorder_payload.append({"id": r["id"], "order": new_order})

    root._drag_item = None
    root._drag_target = None

    # Optimistic update — re-render immediately with new order
    global _cached_reminders
    _cached_reminders = ordered
    _populate_list(ordered, is_cache=False)

    # Sync to server in background (no rebuild needed on success)
    port = _port
    def bg():
        _api_post(port, "/api/reminders/reorder", reorder_payload)
    threading.Thread(target=bg, daemon=True).start()


def _animate_remove(row, on_done):
    """Animate a row removal: dim + fade to background, then callback."""
    root = _root
    if not root:
        on_done()
        return

    # BG color as RGB tuple for interpolation target
    # BG = "#1a1a2e" -> (26, 26, 46)
    bg_r, bg_g, bg_b = 26, 26, 46
    # Start color: reddish tint to signal removal
    start_r, start_g, start_b = 60, 24, 30

    steps = 6
    delay = 50

    # Step 0: instantly dim row with red tint, grey out text and hide buttons
    def _apply_to_children(bg_hex, fg_hex):
        for child in row.winfo_children():
            try:
                child.configure(bg=bg_hex)
                if isinstance(child, tk.Label):
                    child.configure(fg=fg_hex)
                elif isinstance(child, tk.Button):
                    child.configure(state="disabled", bg=bg_hex, fg=bg_hex)
            except tk.TclError:
                pass

    try:
        row.configure(bg=f"#{start_r:02x}{start_g:02x}{start_b:02x}")
        _apply_to_children(f"#{start_r:02x}{start_g:02x}{start_b:02x}", "#666666")
    except tk.TclError:
        on_done()
        return

    def fade(step):
        if step >= steps:
            on_done()
            return
        t = (step + 1) / steps  # 0→1 progress
        r = int(start_r + (bg_r - start_r) * t)
        g = int(start_g + (bg_g - start_g) * t)
        b = int(start_b + (bg_b - start_b) * t)
        fg_val = max(0, int(102 * (1 - t)))
        bg_hex = f"#{r:02x}{g:02x}{b:02x}"
        fg_hex = f"#{fg_val:02x}{fg_val:02x}{fg_val:02x}"
        try:
            row.configure(bg=bg_hex)
            _apply_to_children(bg_hex, fg_hex)
        except tk.TclError:
            on_done()
            return
        root.after(delay, lambda: fade(step + 1))

    root.after(delay, lambda: fade(0))


def _populate_list(reminders, is_cache=False):
    """Populate the list with fetched reminders. Called on the Tk thread."""
    global popup_visible
    root = _root
    if not root:
        return

    content_frame = root._content_frame
    port = _port

    for widget in content_frame.winfo_children():
        widget.destroy()

    if not reminders:
        if is_cache:
            # Don't act on empty cache — wait for fresh data
            return
        global _auto_hide_id
        _resize_to_fit(0)
        tk.Label(content_frame, text="You DID it!!!", font=root._font_done, bg=BG, fg="#2ecc71").pack(pady=40)
        if _auto_hide_id:
            root.after_cancel(_auto_hide_id)
        _auto_hide_id = root.after(2000, _hide_popup)
        return

    _resize_to_fit(len(reminders))

    item_font = root._font_item
    btn_font = root._font_btn
    handle_font = root._font_handle

    # Reset drag state
    root._drag_rows = []
    root._drag_reminders = list(reminders)
    root._drag_item = None
    root._drag_target = None

    total = len(reminders)
    for idx, reminder in enumerate(reminders):
        row = tk.Frame(content_frame, bg=CARD_BG, pady=6, padx=8)
        row.pack(fill="x", pady=3, padx=4)
        root._drag_rows.append(row)

        # Drag handle (visual indicator only)
        handle = tk.Label(row, text="\u2630", font=handle_font, bg=CARD_BG, fg="#666688",
                          padx=2)
        handle.pack(side="left", padx=(0, 6))

        text_label = tk.Label(row, text=reminder["text"], font=item_font,
                 bg=CARD_BG, fg=FG, anchor="w", wraplength=390, justify="left")
        text_label.pack(side="left", fill="x", expand=True)

        # Bind drag events to entire row and its children
        def make_drag_start(i):
            def handler(e):
                _drag_start(e, i)
            return handler
        for widget in (row, handle, text_label):
            widget.bind("<Button-1>", make_drag_start(idx))
            widget.bind("<B1-Motion>", lambda e: _drag_motion(e))
            widget.bind("<ButtonRelease-1>", lambda e: _drag_end(e))
            widget.configure(cursor="hand2")

        rid = reminder["id"]

        def make_complete(r_id, r_text, r_row):
            def do_complete():
                def after_anim():
                    def bg():
                        _api_call(port, f"/api/reminders/{r_id}/complete", "PATCH")
                        root.after(0, _rebuild_list)
                        root.after(300, lambda: _show_undo("complete", r_id, r_text))
                    threading.Thread(target=bg, daemon=True).start()
                _animate_remove(r_row, after_anim)
            return do_complete

        tk.Button(row, text="\u2713", font=btn_font, bg="#2ecc71", fg="white",
                  bd=0, width=3, cursor="hand2", command=make_complete(rid, reminder["text"], row)).pack(side="right", padx=(4, 0))

        def make_menu(r_id, r_text, widget, r_row):
            def show_menu():
                menu = tk.Menu(root, tearoff=0, bg=CARD_BG, fg=FG,
                               activebackground=ACCENT, activeforeground="white")

                def do_edit():
                    _show_edit_dialog(root, port, r_id, r_text)

                def do_delete():
                    def after_anim():
                        def bg():
                            _api_delete(port, f"/api/reminders/{r_id}")
                            root.after(0, _rebuild_list)
                            root.after(300, lambda: _show_undo("delete", r_id, r_text))
                        threading.Thread(target=bg, daemon=True).start()
                    _animate_remove(r_row, after_anim)

                def do_copy():
                    root.clipboard_clear()
                    root.clipboard_append(r_text)

                menu.add_command(label="Copy", command=do_copy)
                menu.add_command(label="Edit", command=do_edit)
                menu.add_command(label="Delete", command=do_delete)

                try:
                    menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())
                finally:
                    menu.grab_release()
            return show_menu

        dots_btn = tk.Button(row, text="\u22EE", font=btn_font, bg=BTN_BG, fg=FG,
                             bd=0, width=2, cursor="hand2")
        dots_btn.configure(command=make_menu(rid, reminder["text"], dots_btn, row))
        dots_btn.pack(side="right", padx=(4, 0))


def _show_edit_dialog(parent, port, r_id, current_text):
    dialog = tk.Toplevel(parent)
    dialog.overrideredirect(True)
    dialog.attributes("-topmost", True)
    dialog.configure(bg=BG)
    dialog.geometry("300x120+{}+{}".format(parent.winfo_x() + 20, parent.winfo_y() + 60))

    tk.Label(dialog, text="Edit reminder:", bg=BG, fg=FG, font=("Segoe UI", 10)).pack(padx=10, pady=(10, 4), anchor="w")

    entry = tk.Entry(dialog, font=("Segoe UI", 11), bg=CARD_BG, fg=FG, insertbackground=FG)
    entry.insert(0, current_text)
    entry.pack(fill="x", padx=10, pady=4)
    entry.focus_set()

    btn_frame = tk.Frame(dialog, bg=BG)
    btn_frame.pack(fill="x", padx=10, pady=8)

    def save():
        new_text = entry.get().strip()
        if new_text and new_text != current_text:
            def bg():
                _api_put(port, f"/api/reminders/{r_id}", {"text": new_text})
                _root.after(0, _rebuild_list)
            threading.Thread(target=bg, daemon=True).start()
        dialog.destroy()

    def cancel():
        dialog.destroy()

    tk.Button(btn_frame, text="Save", bg=ACCENT, fg="white", bd=0, font=("Segoe UI", 10),
              command=save, cursor="hand2").pack(side="left", padx=(0, 8))
    tk.Button(btn_frame, text="Cancel", bg=CARD_BG, fg=FG, bd=0, font=("Segoe UI", 10),
              command=cancel, cursor="hand2").pack(side="left")

    entry.bind("<Return>", lambda e: save())
    entry.bind("<Escape>", lambda e: cancel())


def start_popup_thread():
    """Start the persistent Tk thread. Call once at app startup."""
    thread = threading.Thread(target=_init_window, daemon=True)
    thread.start()
    _initialized.wait(timeout=10)


_auto_refresh_id = None
AUTO_REFRESH_MS = 30000  # Refresh every 30s while popup is visible


def _auto_refresh():
    """Periodically refresh the list while the popup is visible."""
    global _auto_refresh_id
    if not popup_visible or not _root:
        _auto_refresh_id = None
        return
    _rebuild_list()
    _auto_refresh_id = _root.after(AUTO_REFRESH_MS, _auto_refresh)


def show_popup(port=5123):
    """Show the popup window. Instant since window already exists."""
    global popup_visible, _port, _auto_refresh_id
    _port = port

    with _popup_lock:
        if popup_visible:
            return
        popup_visible = True

    if not _initialized.is_set():
        start_popup_thread()

    # Show window and refresh content — schedule on the Tk thread
    def do_show():
        global _auto_refresh_id, _auto_hide_id
        # Cancel any pending auto-hide from a previous "You DID it!!!" state
        if _auto_hide_id:
            _root.after_cancel(_auto_hide_id)
            _auto_hide_id = None
        _root.deiconify()
        _root.lift()
        _root.attributes("-topmost", True)
        _rebuild_list()
        # Start auto-refresh loop
        if _auto_refresh_id is None:
            _auto_refresh_id = _root.after(AUTO_REFRESH_MS, _auto_refresh)

    _root.after(0, do_show)
