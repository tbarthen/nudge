"""
Nudge — DO IT!!!
Entry point: starts Flask server in a daemon thread, then runs pystray on the main thread.
"""
import atexit
import ctypes
import signal
import sys
import threading

from server import app, init_data_file
from tray import run_tray, _get_config
from popup import start_popup_thread

MUTEX_NAME = "NudgeDoItSingleInstance"
_mutex_handle = None


def _acquire_single_instance():
    """Prevent multiple instances using a Windows named mutex."""
    global _mutex_handle
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, True, MUTEX_NAME)
    last_error = ctypes.windll.kernel32.GetLastError()
    if last_error == 183:  # ERROR_ALREADY_EXISTS
        print("Nudge is already running.")
        sys.exit(0)
    atexit.register(_release_mutex)
    return _mutex_handle


def _release_mutex():
    """Release the Windows named mutex on exit."""
    if _mutex_handle:
        ctypes.windll.kernel32.ReleaseMutex(_mutex_handle)
        ctypes.windll.kernel32.CloseHandle(_mutex_handle)


def main():
    # Single instance check
    _acquire_single_instance()

    # Ensure data file exists
    init_data_file()

    config = _get_config()
    port = config.get("server_port", 5123)

    stop_event = threading.Event()

    # Start Flask in a daemon thread
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False),
        daemon=True,
    )
    flask_thread.start()

    # Pre-create the popup window (hidden) so clicks are instant
    start_popup_thread()

    # Handle Ctrl+C gracefully
    def sigint_handler(sig, frame):
        stop_event.set()
        sys.exit(0)

    signal.signal(signal.SIGINT, sigint_handler)

    # Run tray on main thread (pystray requires it)
    try:
        run_tray(stop_event)
    except KeyboardInterrupt:
        stop_event.set()


if __name__ == "__main__":
    main()
