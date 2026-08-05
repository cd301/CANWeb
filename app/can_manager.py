"""
CANWeb – python-can bus manager.

Wraps python-can Bus objects and provides thread-safe access for the
Flask application.  A virtual (socketcan-based) bus is used when no real
hardware is available so the app can run without any dongle attached.
"""

from __future__ import annotations

import importlib
import threading
import time
import random
import sys
from typing import Optional, Callable

import can


# ──────────────────────────────────────────────────────────────────────────────
# Simulated bus (used when no hardware is present)
# ──────────────────────────────────────────────────────────────────────────────

class _SimulatedBus:
    """Mimics python-can Bus interface, emitting randomised CAN frames."""

    CHANNEL = "virtual"

    def __init__(self):
        self._stop = threading.Event()
        self._listeners: list[Callable] = []
        self._lock = threading.Lock()
        # Stats – must be set before thread starts
        self.rx_count = 0
        self.error_count = 0
        self.bitrate = 500_000
        self._start_time = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # IDs that we simulate (realistic-ish)
    _ARBIT_IDS = [0x100, 0x200, 0x300, 0x400, 0x18FF1234, 0x18FF5678]

    def _run(self):
        while not self._stop.is_set():
            arb_id = random.choice(self._ARBIT_IDS)
            dlc = random.randint(1, 8)
            data = bytes([random.randint(0, 255) for _ in range(dlc)])
            is_extended = arb_id > 0x7FF
            msg = can.Message(
                arbitration_id=arb_id,
                data=data,
                is_extended_id=is_extended,
                timestamp=time.time(),
            )
            with self._lock:
                self.rx_count += 1
                for cb in self._listeners:
                    try:
                        cb(msg)
                    except Exception:
                        pass
            time.sleep(random.uniform(0.02, 0.1))

    def add_listener(self, cb: Callable):
        with self._lock:
            self._listeners.append(cb)

    def remove_listener(self, cb: Callable):
        with self._lock:
            if cb in self._listeners:
                self._listeners.remove(cb)

    def send(self, msg: can.Message):
        with self._lock:
            for cb in self._listeners:
                try:
                    cb(msg)
                except Exception:
                    pass

    def shutdown(self):
        self._stop.set()

    def bus_load(self) -> float:
        elapsed = time.monotonic() - self._start_time
        if elapsed == 0:
            return 0.0
        # Rough estimate: each frame is ~(dlc+8)*10 bits at self.bitrate
        avg_bits = 10 * 8 * 10  # ~80 bits per frame average
        total_bits = self.rx_count * avg_bits
        capacity_bits = self.bitrate * elapsed
        return min(100.0, total_bits / capacity_bits * 100)


# ──────────────────────────────────────────────────────────────────────────────
# Real bus wrapper
# ──────────────────────────────────────────────────────────────────────────────

class _RealBus:
    """Thin wrapper around a python-can Bus with listener management."""

    def __init__(self, interface: str, channel: str, bitrate: int, **kwargs):
        self._bus = can.Bus(
            interface=interface,
            channel=channel,
            bitrate=bitrate,
            **kwargs,
        )
        self._lock = threading.Lock()
        self._listeners: list[Callable] = []
        self.rx_count = 0
        self.error_count = 0
        self.bitrate = bitrate
        self._start_time = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                msg = self._bus.recv(timeout=0.1)
            except Exception:
                time.sleep(0.05)
                continue
            if msg is None:
                continue
            with self._lock:
                if msg.is_error_frame:
                    self.error_count += 1
                else:
                    self.rx_count += 1
                for cb in self._listeners:
                    try:
                        cb(msg)
                    except Exception:
                        pass

    def add_listener(self, cb: Callable):
        with self._lock:
            self._listeners.append(cb)

    def remove_listener(self, cb: Callable):
        with self._lock:
            if cb in self._listeners:
                self._listeners.remove(cb)

    def send(self, msg: can.Message):
        self._bus.send(msg)

    def shutdown(self):
        self._stop.set()
        self._bus.shutdown()

    def bus_load(self) -> float:
        elapsed = time.monotonic() - self._start_time
        if elapsed == 0:
            return 0.0
        avg_bits = 10 * 8 * 10
        total_bits = self.rx_count * avg_bits
        capacity_bits = self.bitrate * elapsed
        return min(100.0, total_bits / capacity_bits * 100)


# ──────────────────────────────────────────────────────────────────────────────
# Public manager
# ──────────────────────────────────────────────────────────────────────────────

_bus_instance = None
_bus_lock = threading.Lock()


def get_bus():
    return _bus_instance


def _diagnose_error(interface: str, channel: str, exc: Exception) -> str:
    """Return a human-readable diagnostic message for a failed CAN connection."""
    err = str(exc)
    iface_lower = interface.lower()

    # ── PCAN ──────────────────────────────────────────────────────────────────
    if iface_lower == "pcan":
        # Check if the pcan Python package is installed
        try:
            importlib.import_module("uptime")  # pcan backend uses ctypes, not a separate package
        except ImportError:
            pass
        try:
            importlib.import_module("PCANBasic")
        except ImportError:
            return (
                "PCAN: The PCANBasic library is not installed or not found on PATH. "
                "Install PEAK System PCAN drivers from https://www.peak-system.com/Software.68.0.html "
                "and ensure PCANBasic.dll (Windows) or libpcanbasic.so (Linux) is accessible."
            )
        if "PCAN_ERROR_NODRIVER" in err or "no driver" in err.lower():
            return (
                f"PCAN: Driver not loaded for channel '{channel}'. "
                "Ensure PEAK PCAN drivers are installed and the device is plugged in. "
                "On Linux, run: sudo modprobe peak_usb"
            )
        if "PCAN_ERROR_BUSOFF" in err or "bus off" in err.lower():
            return (
                f"PCAN: Channel '{channel}' is in Bus-Off state. "
                "Check CAN bus wiring, termination resistors (120Ω at each end), "
                "and that at least one other node is active on the bus."
            )
        if "PCAN_ERROR_INITIALIZE" in err or "not initialized" in err.lower():
            return (
                f"PCAN: Channel '{channel}' could not be initialised. "
                "Ensure the device is connected via USB, not already opened by another application "
                "(e.g. PEAK PCAN-View), and that the channel name is correct (e.g. PCAN_USBBUS1)."
            )
        if "PCAN_ERROR_ILLHW" in err or "illegal hardware" in err.lower():
            return (
                f"PCAN: Illegal hardware handle for channel '{channel}'. "
                "Valid channel names are PCAN_USBBUS1 … PCAN_USBBUS8 for USB devices."
            )
        return (
            f"PCAN: Could not open channel '{channel}'. "
            f"Raw error: {err}. "
            "Check: device is plugged in, drivers are installed, channel name is correct, "
            "and no other application has the channel open."
        )

    # ── Kvaser ────────────────────────────────────────────────────────────────
    if iface_lower == "kvaser":
        try:
            importlib.import_module("canlib")
        except ImportError:
            return (
                "Kvaser: The 'canlib' Python package is not installed. "
                "Install Kvaser drivers and CANlib SDK from https://www.kvaser.com/downloads-kvaser/ "
                "then install the Python wrapper: pip install canlib"
            )
        if "canERR_NOTFOUND" in err or "not found" in err.lower():
            return (
                f"Kvaser: No Kvaser device found on channel '{channel}'. "
                "Ensure the device is plugged in, Kvaser drivers are installed, "
                "and the channel index is correct (0-based: channel 0 = first device)."
            )
        if "canERR_NOCHANNELS" in err or "no channels" in err.lower():
            return (
                "Kvaser: No Kvaser CAN channels found. "
                "Plug in a Kvaser device and install the Kvaser drivers."
            )
        if "canERR_INVHANDLE" in err or "invalid handle" in err.lower():
            return (
                f"Kvaser: Invalid channel handle for channel '{channel}'. "
                "Kvaser channels are 0-based integers (e.g. 0 for the first channel)."
            )
        return (
            f"Kvaser: Could not open channel '{channel}'. "
            f"Raw error: {err}. "
            "Check: device is plugged in, Kvaser CANlib drivers are installed, "
            "channel index is correct (0-based integer), and bitrate matches the bus."
        )

    # ── SocketCAN ─────────────────────────────────────────────────────────────
    if iface_lower == "socketcan":
        if sys.platform != "linux":
            return (
                "SocketCAN is only available on Linux. "
                "On Windows or macOS, use a USB CAN adapter with its own driver (PCAN, Kvaser, etc.)."
            )
        if "no such device" in err.lower() or "network interface" in err.lower():
            return (
                f"SocketCAN: Interface '{channel}' not found. "
                f"Run: ip link show  to list available interfaces. "
                f"Bring up the interface with: sudo ip link set {channel} type can bitrate 500000 && sudo ip link set {channel} up"
            )
        return (
            f"SocketCAN: Could not open '{channel}'. "
            f"Raw error: {err}. "
            "Ensure the interface is up: sudo ip link set <iface> up"
        )

    # ── Generic fallback ──────────────────────────────────────────────────────
    return (
        f"Failed to connect to CAN interface '{interface}' on channel '{channel}'. "
        f"Error: {err}. "
        "Check that the required drivers and Python packages are installed for this interface."
    )


def connect(interface: str = "virtual", channel: str = "virtual",
            bitrate: int = 500_000, **kwargs):
    global _bus_instance
    with _bus_lock:
        if _bus_instance is not None:
            _bus_instance.shutdown()
        if interface in ("virtual", "sim"):
            _bus_instance = _SimulatedBus()
        else:
            try:
                _bus_instance = _RealBus(interface, channel, bitrate, **kwargs)
            except Exception as exc:
                _bus_instance = None
                raise ConnectionError(_diagnose_error(interface, channel, exc)) from exc
    return _bus_instance


def disconnect():
    global _bus_instance
    with _bus_lock:
        if _bus_instance is not None:
            _bus_instance.shutdown()
            _bus_instance = None
