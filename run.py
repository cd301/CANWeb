"""CANWeb – entry point."""

import threading
import time
import webbrowser

from app import app, socketio

_PORT = 5000


def _open_browser():
    """Wait briefly for Flask to start, then open the browser."""
    time.sleep(1.2)
    webbrowser.open(f"http://localhost:{_PORT}")


if __name__ == "__main__":
    threading.Thread(target=_open_browser, daemon=True).start()
    socketio.run(app, host="0.0.0.0", port=_PORT, debug=False, allow_unsafe_werkzeug=True)
