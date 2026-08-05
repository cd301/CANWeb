"""CANWeb – entry point."""

import threading
import time
import webbrowser

from app import app, socketio

def _open_browser():
    """Open the browser after a short delay to let the server start."""
    time.sleep(1.0)
    webbrowser.open("http://127.0.0.1:5000")

if __name__ == "__main__":
    threading.Thread(target=_open_browser, daemon=True).start()
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)
