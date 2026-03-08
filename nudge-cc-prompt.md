# Nudge — CC Build Prompt

Begin by running the start-session skill.

Plan Mode → Execute. Push code, run tests, make implementation decisions. Only stop if a subjective design decision isn't covered below.

**Task:** Build "Nudge" — a local daily reminder system with a Flask web UI, Windows system tray icon, and hourly tkinter popup overlay.

**Why:** The user needs non-dismissable hourly reminders that require deliberate interaction to close. Standard Windows notifications are too easy to ignore. The web UI enables future cross-device access from a phone.

**Branding:** The app's identity is **"DO IT!!!"** — this tagline appears as a bold, persistent header/title in both the web UI and the tkinter popup. It's the personality of the app: no coddling, no snooze buttons, just get it done. When all reminders are completed, the empty state message is **"You DID it!!!"**

**Project location:** `C:\DEV\nudge`

**Stack:** Python 3.x — Flask, pystray, Pillow, tkinter (stdlib), ctypes (stdlib)

---

## Architecture — 5 files + 1 template

### 1. `launcher.py`
Entry point. Starts Flask server in a daemon thread, then starts pystray on the main thread (pystray requires main thread). Creates `reminders.json` with sensible defaults if it doesn't exist. Handles graceful shutdown via tray Quit or Ctrl+C.

### 2. `server.py`
Flask app. REST API for reminders (full CRUD), plus `/api/config` GET/PUT for settings. Serves `templates/index.html` at `/`. Data persisted to `reminders.json` (JSON file, not SQLite). Port configurable in config (default 5123).

**API design:**
- `GET /api/reminders` — active reminders
- `POST /api/reminders` — add (body: `{text, pinned?}`)
- `PUT /api/reminders/<id>` — edit
- `DELETE /api/reminders/<id>` — delete
- `PATCH /api/reminders/<id>/complete` — mark complete, move to completed list
- `GET/PUT /api/config` — popup interval (minutes), server port, start_with_windows flag

### 3. `tray.py`
System tray icon via pystray. Left-click opens `http://localhost:<port>` in default browser. Right-click menu: Show Reminders (triggers popup), Open Web UI, Quit. Runs a timer thread that fires every `popup_interval_minutes`.

**Anti-duplicate logic:** maintains a `popup_visible` flag — if True, skip. Idle detection via `ctypes.windll.user32.GetLastInputInfo` — if system has been idle longer than the popup interval, do NOT stack popups; just show one on next user input. The tray module imports and calls popup functions.

### 4. `popup.py`
Tkinter always-on-top overlay. Small borderless window anchored to bottom-right of screen (above taskbar — detect taskbar position via ctypes). **"DO IT!!!"** displayed as the bold popup title/header. Fetches active reminders from the Flask API on display.

Each row shows: reminder text, a ✓ button (complete via API, remove row), and a ⋮ three-dot menu (Edit, Delete, Pin to top). X button in corner hides the window (does NOT snooze or delay the next timer cycle — just sets `popup_visible = False`). When all items are completed, show **"You DID it!!!"** briefly then auto-hide. Single instance enforced — never two popup windows at once.

### 5. `templates/index.html`
Single-file web UI (inline CSS + JS, no build step, no framework). Responsive for future phone use. **"DO IT!!!"** as the bold persistent page title at top.

**Sections:** reminder list with inline edit + complete + delete, add-reminder input at top, collapsible "Completed" section (last 7 days) with **"You DID it!!!"** as its header, settings panel (popup interval dropdown: 15m/30m/45m/60m/90m/2h, theme toggle light/dark/system). Uses fetch() against the Flask API. Clean, modern design — minimal and functional, with the "DO IT!!!" branding giving it attitude.

### 6. `requirements.txt`
flask>=3.0, pystray>=0.19, Pillow>=10.0

---

## Critical Behaviors

- All data mutations (even from the tkinter popup) go through the Flask API — keeps web UI and popup in sync.
- Popup timer resets cleanly after idle. If the user is away 3 hours, they get ONE popup on return, not three.
- The X button on the popup is purely a hide action. The user may complete tasks right after hiding it, so they can left-click the tray icon or right-click → Show Reminders to bring it back anytime.
- Generate a simple tray icon programmatically using Pillow (a small bell or exclamation mark that fits the "DO IT!!!" energy) so there's no external asset dependency. Save it as `icon.ico` on first run if it doesn't exist.

## Data Model — `reminders.json`

```json
{
  "config": {
    "popup_interval_minutes": 60,
    "server_port": 5123,
    "start_with_windows": false
  },
  "reminders": [
    {
      "id": "uuid",
      "text": "Stretch and walk around",
      "created_at": "ISO-8601",
      "completed_at": null,
      "pinned": false,
      "order": 0
    }
  ],
  "completed": []
}
```

---

## Validation

Launch the app, confirm:
1. Tray icon appears
2. Left-click opens web UI in browser with "DO IT!!!" title visible
3. Web UI can add/complete/delete reminders
4. Popup appears as an always-on-top overlay after the configured interval (for testing set to 1 minute) with "DO IT!!!" header
5. Closing popup via X and reopening via tray works
6. Completing all items in popup shows "You DID it!!!" and auto-hides
