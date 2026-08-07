"""
CANWeb – Flask + Flask-SocketIO application factory.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import tempfile
import threading
import time
from typing import Any

import can
import yaml
from flask import (Flask, jsonify, redirect, render_template, request,
                   send_file, url_for)
from flask_socketio import SocketIO, emit

from . import can_manager, dbc_manager

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "canweb-secret")
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")
if not app.logger.handlers:
    logging.basicConfig(
        level=os.environ.get("CANWEB_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
app.logger.setLevel(os.environ.get("CANWEB_LOG_LEVEL", "INFO").upper())
_log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# In-memory ring buffer of raw frames (for export / history)
# ──────────────────────────────────────────────────────────────────────────────

MAX_BUFFER = 10_000
_buffer: list[dict] = []
_buffer_lock = threading.Lock()

# Signal time-series store  {signal_name: [(timestamp, value), ...]}
_signal_data: dict[str, list] = {}
_signal_lock = threading.Lock()

# Stats
_stats = {"rx": 0, "errors": 0, "bus_load": 0.0, "bitrate": 500_000}


def _store_decoded_signal(ts: float, decoded: dict[str, Any]):
    with _signal_lock:
        for sig, val in decoded["signals"].items():
            key = f"{decoded['name']}.{sig}"
            if key not in _signal_data:
                _signal_data[key] = []
            _signal_data[key].append((ts, val))
            if len(_signal_data[key]) > 5000:
                _signal_data[key] = _signal_data[key][-5000:]


def _redecode_buffered_frames():
    with _buffer_lock:
        frames = list(_buffer)
    rebuilt_signal_data: dict[str, list] = {}
    for frame in frames:
        try:
            arb_id = int(frame["id"], 16)
            data = bytes.fromhex(frame["data"])
        except (KeyError, ValueError, TypeError):
            continue
        decoded = dbc_manager.decode_message(arb_id, data, frame.get("ext"))
        if decoded:
            frame["decoded"] = decoded
            for sig, val in decoded["signals"].items():
                key = f"{decoded['name']}.{sig}"
                rebuilt_signal_data.setdefault(key, []).append((frame["ts"], val))
        else:
            frame.pop("decoded", None)
    with _buffer_lock:
        _buffer[:] = frames
    with _signal_lock:
        _signal_data.clear()
        _signal_data.update(rebuilt_signal_data)


def _on_message(msg: can.Message):
    """Called from the CAN receive thread for every incoming frame."""
    ts = msg.timestamp or time.time()
    hex_data = msg.data.hex().upper()
    arb_id = msg.arbitration_id
    frame: dict[str, Any] = {
        "ts": round(ts, 6),
        "id": f"0x{arb_id:X}",
        "dlc": msg.dlc,
        "data": hex_data,
        "ext": msg.is_extended_id,
        "error": msg.is_error_frame,
    }

    # Try DBC decode
    decoded = dbc_manager.decode_message(arb_id, bytes(msg.data), msg.is_extended_id)
    if decoded:
        frame["decoded"] = decoded
        _store_decoded_signal(ts, decoded)

    with _buffer_lock:
        _buffer.append(frame)
        if len(_buffer) > MAX_BUFFER:
            del _buffer[0]

    socketio.emit("can_frame", frame)


# Register callback with the default simulated bus
can_manager.get_bus().add_listener(_on_message)


# ──────────────────────────────────────────────────────────────────────────────
# Background stats emitter
# ──────────────────────────────────────────────────────────────────────────────

def _stats_loop():
    while True:
        time.sleep(1)
        bus = can_manager.get_bus()
        if bus:
            _stats["rx"] = bus.rx_count
            _stats["errors"] = bus.error_count
            _stats["bus_load"] = round(bus.bus_load(), 2)
            _stats["bitrate"] = bus.bitrate
        socketio.emit("stats", _stats)


_stats_thread = threading.Thread(target=_stats_loop, daemon=True)
_stats_thread.start()

try:
    dbc_manager.load_dbc(dbc_manager.default_dbc_path())
except Exception:
    _log.exception("Failed to load bundled default DBC")


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── CAN interface ──────────────────────────────────────────────────────────────

@app.route("/api/connect", methods=["POST"])
def api_connect():
    data = request.json or {}
    interface = data.get("interface", "sim")
    channel = data.get("channel", "virtual")
    bitrate = int(data.get("bitrate", 500_000))

    old_bus = can_manager.get_bus()
    if old_bus:
        old_bus.remove_listener(_on_message)

    try:
        bus = can_manager.connect(interface, channel, bitrate)
        bus.add_listener(_on_message)
        return jsonify({"ok": True, "interface": interface, "channel": channel, "bitrate": bitrate})
    except Exception as exc:
        # Log the full exception server-side for debugging, but only return a
        # sanitised type+message to the client to avoid exposing internal paths.
        _log.exception("CAN connect failed: interface=%s channel=%s", interface, channel)
        exc_type = type(exc).__name__
        exc_msg = str(exc)
        # Strip any absolute file paths from the message before sending to client
        import re as _re
        exc_msg = _re.sub(r'(?:/[\w./\\-]+)', '<path>', exc_msg)
        safe_msg = f"{exc_type}: {exc_msg}" if exc_msg else "Failed to connect to CAN interface"
        return jsonify({"ok": False, "error": safe_msg}), 400


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    bus = can_manager.get_bus()
    if bus:
        bus.remove_listener(_on_message)
    can_manager.disconnect()
    return jsonify({"ok": True})


# ── DBC ───────────────────────────────────────────────────────────────────────

@app.route("/api/dbc/upload", methods=["POST"])
def api_dbc_upload():
    files = request.files.getlist("file")
    if not files or all(f.filename == "" for f in files):
        return jsonify({"ok": False, "error": "No file"}), 400

    # When multiple files are sent in one request, stack them all.
    # When a single file is sent, honour the explicit 'append' param (default false).
    multiple = len(files) > 1
    append_param = request.form.get("append", "false").lower() not in ("false", "0", "no")

    info = None
    tmp_paths = []
    try:
        for i, f in enumerate(files):
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".dbc")
            tmp.close()
            tmp_paths.append(tmp.name)
            f.save(tmp.name)
            append = (multiple and i > 0) or (not multiple and append_param)
            info = dbc_manager.load_dbc(tmp.name, append=append)
        _redecode_buffered_frames()
        return jsonify({"ok": True, **info})
    except Exception:
        _log.exception("DBC upload failed")
        return jsonify({"ok": False, "error": "Failed to parse DBC file"}), 400
    finally:
        for p in tmp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass


@app.route("/api/dbc/info")
def api_dbc_info():
    info = dbc_manager.get_db_info()
    if info is None:
        return jsonify({"loaded": False})
    return jsonify({"loaded": True, **info})


@app.route("/api/dbc/clear", methods=["POST"])
def api_dbc_clear():
    dbc_manager.clear_dbc()
    with _signal_lock:
        _signal_data.clear()
    with _buffer_lock:
        for frame in _buffer:
            frame.pop("decoded", None)
    return jsonify({"ok": True})


# ── Transmit ──────────────────────────────────────────────────────────────────

@app.route("/api/transmit", methods=["POST"])
def api_transmit():
    data = request.json or {}
    try:
        arb_id = int(data.get("id", "0x100"), 16)
        raw_data = bytes.fromhex(data.get("data", "").replace(" ", ""))
        is_ext = bool(data.get("extended", arb_id > 0x7FF))
        msg = can.Message(arbitration_id=arb_id, data=raw_data, is_extended_id=is_ext)
        bus = can_manager.get_bus()
        if bus is None:
            return jsonify({"ok": False, "error": "Not connected"}), 400
        bus.send(msg)
        return jsonify({"ok": True})
    except Exception:
        _log.exception("CAN transmit failed")
        return jsonify({"ok": False, "error": "Failed to transmit frame"}), 400


# ── Signal data ───────────────────────────────────────────────────────────────

@app.route("/api/signals")
def api_signals():
    # Start with signals that have live data
    with _signal_lock:
        known = set(_signal_data.keys())
    # Also include all signals defined in the loaded DBC so the UI can
    # populate plot/signal selectors immediately after a DBC upload, even
    # before any matching frames have been received.
    db_info = dbc_manager.get_db_info()
    if db_info:
        for msg in db_info["messages"]:
            for sig in msg["signals"]:
                known.add(f"{msg['name']}.{sig['name']}")
    return jsonify(sorted(known))


@app.route("/api/signals/<path:signal_name>")
def api_signal_data(signal_name):
    with _signal_lock:
        pts = _signal_data.get(signal_name, [])
        return jsonify({"signal": signal_name, "points": pts})


# ── Export ────────────────────────────────────────────────────────────────────

@app.route("/api/export/csv")
def api_export_csv():
    with _buffer_lock:
        frames = list(_buffer)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp", "id", "dlc", "data", "extended", "error", "decoded_name", "signal_name", "signal_value"])
    for f in frames:
        dec = f.get("decoded", {})
        signals = dec.get("signals", {}) if dec else {}
        if signals:
            for sig, val in signals.items():
                writer.writerow([
                    f["ts"], f["id"], f["dlc"], f["data"],
                    f.get("ext", False), f.get("error", False),
                    dec.get("name", ""), sig, val,
                ])
        else:
            writer.writerow([
                f["ts"], f["id"], f["dlc"], f["data"],
                f.get("ext", False), f.get("error", False),
                dec.get("name", "") if dec else "", "", "",
            ])
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode()),
        mimetype="text/csv",
        as_attachment=True,
        download_name="canweb_export.csv",
    )


# ── Config save / load ────────────────────────────────────────────────────────

@app.route("/api/config/save", methods=["POST"])
def api_config_save():
    data = request.json or {}
    fmt = data.get("format", "json")
    config = data.get("config", {})
    # Restrict format to known values only
    if fmt == "yaml":
        content = yaml.dump(config)
        mime = "application/x-yaml"
        download_name = "canweb_config.yaml"
    else:
        # Default to JSON for any unrecognised format value
        fmt = "json"
        content = json.dumps(config, indent=2)
        mime = "application/json"
        download_name = "canweb_config.json"
    buf = io.BytesIO(content.encode("utf-8"))
    return send_file(
        buf,
        mimetype=mime,
        as_attachment=True,
        download_name=download_name,
    )


@app.route("/api/config/load", methods=["POST"])
def api_config_load():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file"}), 400
    f = request.files["file"]
    content = f.read().decode()
    try:
        if f.filename.endswith(".yaml") or f.filename.endswith(".yml"):
            config = yaml.safe_load(content)
        else:
            config = json.loads(content)
        return jsonify({"ok": True, "config": config})
    except Exception:
        _log.exception("Config load failed: filename=%s", f.filename)
        return jsonify({"ok": False, "error": "Failed to parse config file"}), 400


# ── Buffer ────────────────────────────────────────────────────────────────────

@app.route("/api/buffer/clear", methods=["POST"])
def api_buffer_clear():
    with _buffer_lock:
        _buffer.clear()
    with _signal_lock:
        _signal_data.clear()
    return jsonify({"ok": True})


@app.route("/api/buffer/recent")
def api_buffer_recent():
    limit = int(request.args.get("limit", 200))
    with _buffer_lock:
        return jsonify(_buffer[-limit:])


# ── Diagnostics ───────────────────────────────────────────────────────────────

@app.route("/api/diagnose")
def api_diagnose():
    """Return diagnostics about available CAN interfaces and python-can version."""
    import importlib
    import sys
    import platform

    checks: list[dict] = []

    def _check(name: str, import_name: str, detail: str):
        try:
            mod = importlib.import_module(import_name)
            version = getattr(mod, "__version__", "unknown")
            checks.append({"name": name, "ok": True,
                            "detail": f"{detail} (v{version})"})
        except ImportError:
            checks.append({"name": name, "ok": False,
                            "detail": f"{detail} — not installed"})

    _check("python-can", "can", "Core CAN library")
    _check("PCAN driver (python-can pcan)", "can.interfaces.pcan", "Required for PEAK PCAN adapters")
    _check("Kvaser driver (python-can kvaser)", "can.interfaces.kvaser", "Required for Kvaser adapters")
    _check("SocketCAN", "can.interfaces.socketcan", "Built-in Linux SocketCAN support")
    _check("Vector XL", "can.interfaces.vector", "Required for Vector adapters")
    _check("IXXAT VCI", "can.interfaces.ixxat", "Required for IXXAT adapters")
    _check("Serial/SLCAN", "can.interfaces.slcan", "Required for serial SLCAN adapters")

    return jsonify({
        "python": sys.version,
        "platform": platform.platform(),
        "checks": checks,
    })


# ──────────────────────────────────────────────────────────────────────────────
# SocketIO events
# ──────────────────────────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    emit("stats", _stats)


@socketio.on("subscribe_signal")
def on_subscribe_signal(data):
    signal_name = data.get("signal")
    with _signal_lock:
        pts = _signal_data.get(signal_name, [])
    emit("signal_history", {"signal": signal_name, "points": pts})
