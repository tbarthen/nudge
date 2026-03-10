# Nudge — Project Instructions

## Deploying PWA changes

When modifying `docs/index.html` (the GitHub Pages PWA):

1. Mirror the same change in `templates/index.html` (Flask-served desktop UI) unless the change is PWA-only
2. Bump `CACHE_NAME` in `docs/sw.js` (e.g. `nudge-v22` → `nudge-v23`) so phones pick up the update
3. If `static/sw.js` was also changed, bump its `CACHE_NAME` too
4. Commit and **push to GitHub** — the PWA is served from GitHub Pages via the `docs/` folder on `master`

Without the cache bump, phones will keep serving the old cached version indefinitely.

**These steps are mandatory for every change that touches PWA files — always follow them, not just when explicitly asked.**

## Starting a session

Always pull latest from master before making changes: `git checkout master && git pull`

This repo is edited from multiple devices. Pulling first avoids merge conflicts.

## Git workflow

- Always commit and push directly to `master`. Do NOT create feature branches.
- If you cannot push to master (403 error), tell the user immediately — do not retry or create workaround branches.
- Claude Code web UI sessions can only push to `claude/` prefixed branches (platform restriction). If you hit this, commit locally on master and tell the user to push from their VS Code environment.

## Architecture

- **Desktop app**: Python (Flask + pystray + tkinter). Entry point: `launcher.py`
- **PWA**: `docs/` folder served by GitHub Pages. Standalone offline app using IndexedDB
- **Data sync**: Phone pairs with desktop server over local network. Desktop is source of truth
- **Data file**: `reminders.json` (gitignored, auto-created)

## Key conventions

- All data mutations go through the Flask REST API (even from the desktop popup)
- `docs/index.html` and `templates/index.html` are mirrors — keep them in sync
- `docs/sw.js` and `static/sw.js` are mirrors (different path prefixes: relative vs `/static/`)
- Restart command: `taskkill //F //IM pythonw.exe; cd c:/DEV/nudge && pythonw launcher.py`
