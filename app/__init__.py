"""
CANWeb – Flask + Flask-SocketIO application factory.
"""

from __future__ import annotations

import csv
import io
import json
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
    decoded = dbc_manager.decode_message(arb_id, bytes(msg.data))
    if decoded:
        frame["decoded"] = decoded
        with _signal_lock:
            for sig, val in decoded["signals"].items():
                key = f"{decoded['name']}.{sig}"
                if key not in _signal_data:
                    _signal_data[key] = []
                _signal_data[key].append((ts, val))
                # Keep last 5000 points per signal
                if len(_signal_data[key]) > 5000:
                    _signal_data[key] = _signal_data[key][-5000:]

    with _buffer_lock:
        _buffer.append(frame)
        if len(_buffer) > MAX_BUFFER:
            del _buffer[0]

    socketio.emit("can_frame", frame)


# No bus is started automatically; listener is attached on explicit connect.
# can_manager.get_bus().add_listener(_on_message)  -- removed: no auto-sim start


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
        return jsonify({"ok": False, "error": str(exc)}), 400


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
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file"}), 400
    f = request.files["file"]
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".dbc")
    f.save(tmp.name)
    try:
        info = dbc_manager.load_dbc(tmp.name)
        return jsonify({"ok": True, **info})
    except Exception:
        return jsonify({"ok": False, "error": "Failed to parse DBC file"}), 400
    finally:
        os.unlink(tmp.name)


@app.route("/api/dbc/info")
def api_dbc_info():
    info = dbc_manager.get_db_info()
    if info is None:
        return jsonify({"loaded": False})
    return jsonify({"loaded": True, **info})


@app.route("/api/dbc/clear", methods=["POST"])
def api_dbc_clear():
    dbc_manager.clear_dbc()
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
        return jsonify({"ok": False, "error": "Failed to transmit frame"}), 400


# ── Signal data ───────────────────────────────────────────────────────────────

@app.route("/api/signals")
def api_signals():
    with _signal_lock:
        return jsonify(list(_signal_data.keys()))


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
    writer.writerow(["timestamp", "id", "dlc", "data", "extended", "error", "decoded_name", "decoded_signals"])
    for f in frames:
        dec = f.get("decoded", {})
        writer.writerow([
            f["ts"], f["id"], f["dlc"], f["data"],
            f.get("ext", False), f.get("error", False),
            dec.get("name", ""), json.dumps(dec.get("signals", {})) if dec else "",
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
        return jsonify({"ok": False, "error": "Failed to parse config file"}), 400


# ── Buffer ────────────────────────────────────────────────────────────────────

@app.route("/api/buffer/clear", methods=["POST"])
def api_buffer_clear():
    with _buffer_lock:
        _buffer.clear()
    with _signal_lock:
        _signal_data.clear()
    return jsonify({"ok": True})


@app.route("/api/diagnose", methods=["POST"])
def api_diagnose():
    """Return diagnostic information about the requested CAN interface."""
    import importlib
    import shutil
    import sys
    import platform

    data = request.json or {}
    interface = data.get("interface", "sim")
    channel = data.get("channel", "virtual")
    bitrate = int(data.get("bitrate", 500_000))

    lines = []
    lines.append(f"=== CANWeb Diagnostics ===")
    lines.append(f"Platform : {platform.system()} {platform.release()} ({platform.machine()})")
    lines.append(f"Python   : {sys.version.split()[0]}")
    lines.append(f"Interface: {interface}  Channel: {channel}  Bitrate: {bitrate}")
    lines.append("")

    # Check python-can
    try:
        import can as _can
        lines.append(f"[OK] python-can {_can.__version__} installed")
    except ImportError:
        lines.append("[FAIL] python-can is NOT installed – run: pip install python-can")

    if interface.lower() == "pcan":
        lines.append("")
        lines.append("── PCAN checks ──")
        try:
            import PCANBasic
            lines.append("[OK] PCANBasic Python module found")
        except ImportError:
            lines.append("[FAIL] PCANBasic Python module not found")
            lines.append("       Install PEAK System PCAN drivers from https://www.peak-system.com/")

        if sys.platform == "win32":
            dll = shutil.which("PCANBasic.dll")
            lines.append(f"[{'OK' if dll else '??'}] PCANBasic.dll on PATH: {dll or 'not found – install PCAN Basic API'}")
        elif sys.platform.startswith("linux"):
            drv = shutil.which("peak_usb") or "not found"
            lines.append(f"     Linux: kernel module peak_usb – load with: sudo modprobe peak_usb")
            import subprocess
            try:
                out = subprocess.check_output(["lsmod"], text=True)
                if "peak_usb" in out:
                    lines.append("[OK] peak_usb kernel module is loaded")
                else:
                    lines.append("[WARN] peak_usb kernel module not loaded – run: sudo modprobe peak_usb")
            except Exception:
                lines.append("[??] Could not check kernel modules (lsmod unavailable)")
        lines.append(f"     Expected channel format: PCAN_USBBUS1 … PCAN_USBBUS8")
        lines.append(f"     Current channel: {channel}")

    elif interface.lower() == "kvaser":
        lines.append("")
        lines.append("── Kvaser checks ──")
        try:
            import canlib
            lines.append("[OK] canlib Python package found")
            try:
                cl = canlib.canlib()
                n = cl.getNumberOfChannels()
                lines.append(f"[OK] canlib reports {n} channel(s) available")
                for i in range(n):
                    try:
                        chi = cl.getChannelData_Name(i)
                        lines.append(f"     Channel {i}: {chi}")
                    except Exception:
                        lines.append(f"     Channel {i}: (could not read name)")
            except Exception as e:
                lines.append(f"[FAIL] canlib initialisation error: {e}")
                lines.append("       Ensure Kvaser CANlib drivers are installed: https://www.kvaser.com/downloads-kvaser/")
        except ImportError:
            lines.append("[FAIL] canlib Python package not installed – run: pip install canlib")
        lines.append(f"     Expected channel format: 0-based integer (0 = first Kvaser channel)")
        lines.append(f"     Current channel: {channel}")

    elif interface.lower() == "socketcan":
        lines.append("")
        lines.append("── SocketCAN checks ──")
        if sys.platform != "linux":
            lines.append(f"[FAIL] SocketCAN requires Linux (current: {sys.platform})")
        else:
            import subprocess
            try:
                out = subprocess.check_output(["ip", "link", "show"], text=True)
                if channel in out:
                    lines.append(f"[OK] Interface '{channel}' found in ip link output")
                else:
                    lines.append(f"[WARN] Interface '{channel}' NOT found in ip link output")
                    lines.append(f"       Available interfaces:")
                    for line in out.splitlines():
                        if ": " in line and not line.startswith(" "):
                            lines.append(f"         {line.strip()}")
                    lines.append(f"       Bring up: sudo ip link set {channel} type can bitrate {bitrate} && sudo ip link set {channel} up")
            except Exception as e:
                lines.append(f"[??] ip link check failed: {e}")

    lines.append("")
    lines.append("── Attempt connection ──")
    try:
        test_bus = can_manager.connect(interface, channel, bitrate)
        lines.append(f"[OK] Successfully connected to {interface} / {channel}")
        test_bus.remove_listener(can_manager._on_message if hasattr(can_manager, '_on_message') else lambda m: None)
    except Exception as exc:
        lines.append(f"[FAIL] Connection failed:")
        for part in str(exc).split(". "):
            lines.append(f"       {part}.")

    return jsonify({"ok": True, "report": "\n".join(lines)})


@app.route("/api/buffer/recent")
def api_buffer_recent():
    limit = int(request.args.get("limit", 200))
    with _buffer_lock:
        return jsonify(_buffer[-limit:])


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
