# Nudge — Daily Reminder System
### *"DO IT!!!"*

**Project:** `C:\DEV\nudge`
**Stack:** Python 3.x (Flask, pystray, tkinter, ctypes)
**Purpose:** Hourly popup reminders with a web UI for cross-device access and a native Windows system tray presence.

**Branding:** The app's identity is built around "DO IT!!!" — this tagline appears as a persistent header/title in both the web UI and the tkinter popup. It sets the tone: no coddling, no snooze buttons, just get it done.

---

## Architecture Overview

```
┌─────────────┐       ┌──────────────┐       ┌────────────────┐
│  launcher.py │──────▶│  server.py   │◀─────▶│  reminders.json│
│  (entry pt)  │──┐   │  (Flask API  │       └────────────────┘
└─────────────┘  │   │   + Web UI)  │
                  │   └──────────────┘
                  │          ▲
                  │          │ HTTP (localhost)
                  │          ▼
                  │   ┌──────────────┐
                  └──▶│   tray.py    │
                      │ (pystray +   │
                      │  popup timer)│
                      └──────┬───────┘
                             │ spawns/refreshes
                             ▼
                      ┌──────────────┐
                      │  popup.py    │
                      │ (tkinter     │
                      │  overlay)    │
                      └──────────────┘
```

---

## Module Breakdown

### 1. `server.py` — Flask Backend + Web UI

**Responsibilities:**
- REST API for reminder CRUD operations
- Serves the web UI (single-page `index.html`)
- Persists data to `reminders.json`
- Exposes configuration endpoint (popup interval, enabled/disabled)

**API Endpoints:**
| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/` | Serve web UI |
| GET | `/api/reminders` | List active reminders |
| POST | `/api/reminders` | Add a reminder |
| PUT | `/api/reminders/<id>` | Edit a reminder |
| DELETE | `/api/reminders/<id>` | Delete a reminder |
| PATCH | `/api/reminders/<id>/complete` | Mark complete |
| GET | `/api/config` | Get current settings |
| PUT | `/api/config` | Update settings (interval, etc.) |

**Data Model — `reminders.json`:**
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

### 2. `tray.py` — System Tray Icon + Timer

**Responsibilities:**
- Creates a system tray icon using `pystray`
- Manages the popup timer (default: 60 min, configurable)
- Left-click: opens web UI in default browser
- Right-click menu: Show Reminders, Open Web UI, Settings, Quit
- Prevents duplicate popups — tracks whether popup window is already visible
- Detects idle time via `ctypes` (`GetLastInputInfo`) to avoid popup buildup after being away
- On resume from idle: shows ONE popup, not a backlog

**Anti-Duplicate Logic:**
- Maintains a `popup_visible` flag
- Timer fires every N minutes; if `popup_visible` is True, skip
- If system idle time > popup interval, reset timer on next input detected
- Single popup instance enforced via a threading lock

### 3. `popup.py` — Tkinter Overlay Window

**Responsibilities:**
- Small, always-on-top, borderless window positioned at bottom-right of screen
- Shows active reminders as a compact scrollable list
- Each reminder row has:
  - Reminder text (truncated with tooltip for long text)
  - ✓ button — marks complete (calls API, removes from list, subtle animation)
  - ⋮ (three-dot) menu — Edit, Delete, Pin to top
- **X button** in top-right hides the popup (sets `popup_visible = False`)
- Window appears with a subtle slide-in animation from the right
- Styled to feel native — dark or light mode matching Windows theme
- Title bar area displays **"DO IT!!!"** in bold as the popup header — always visible
- If zero active reminders, shows a "You DID it!!!" message

**Key Behaviors:**
- Clicking X hides the window but does NOT snooze or delay anything
- Completing an item via ✓ immediately removes it from the list
- If all items completed, shows "You DID it!!!" then auto-hides after 2 seconds
- Popup position avoids taskbar (queries taskbar position via ctypes)

### 4. `templates/index.html` — Web UI (Single File)

**Responsibilities:**
- Full reminder management interface served by Flask
- Responsive design (works on phone browser for future Android access)
- Inline CSS + JS (single file, no build step)

**Sections:**
- **Header:** Bold **"DO IT!!!"** title (always visible, top of page), current time, settings gear icon
- **Reminder List:** Drag-to-reorder, inline edit, complete/delete actions
- **Add Reminder:** Text input + Add button at top
- **Completed:** Collapsible section showing recently completed items (last 7 days)
- **Settings Panel** (slide-out or modal):
  - Popup interval (dropdown: 15m, 30m, 45m, 60m, 90m, 2h)
  - Server port
  - Start with Windows toggle
  - Theme (light/dark/system)

**Tech:** Vanilla JS + CSS. No framework needed. Fetch API calls to Flask backend.

### 5. `launcher.py` — Entry Point

**Responsibilities:**
- Starts Flask server in a background thread
- Starts pystray system tray (blocks on main thread — pystray requires it)
- Handles graceful shutdown (Ctrl+C or tray Quit)
- Creates `reminders.json` with defaults if it doesn't exist

---

## Key Design Decisions

### No Duplicate Popups
The tray timer checks two conditions before spawning a popup:
1. `popup_visible` flag is False
2. System idle time (via `GetLastInputInfo`) is less than the popup interval

If you've been away for 3 hours, you get ONE popup when you return — not three stacked on top of each other.

### Web-First Data Layer
All data mutations go through the Flask API, even from the tkinter popup. This means:
- The web UI and popup are always in sync
- Future Android app just hits the same API (expose via LAN or tunneling)
- No file locking issues between processes

### Single JSON File
SQLite is overkill for a reminder list. JSON is human-readable, easy to back up, and sufficient for dozens of reminders. If the list ever grows to hundreds, migration to SQLite is straightforward.

### Cross-Device Path
Current: `http://localhost:5123` — works on the same machine.
Future options for phone access:
- Same LAN: `http://<PC-IP>:5123`
- Remote: Tailscale, Cloudflare Tunnel, or ngrok
- Push notifications: bolt on ntfy.sh or Pushover from Python

---

## File Tree

```
C:\DEV\nudge\
├── launcher.py          # Entry point
├── server.py            # Flask app + API
├── tray.py              # System tray + timer
├── popup.py             # Tkinter overlay window
├── reminders.json       # Data store (auto-created)
├── icon.ico             # Tray icon (bundled or generated)
├── requirements.txt     # flask, pystray, Pillow
└── templates/
    └── index.html       # Web UI
```

---

## Dependencies

```
flask>=3.0
pystray>=0.19
Pillow>=10.0
```

All other modules (`tkinter`, `ctypes`, `threading`, `json`, `uuid`) are Python stdlib.
