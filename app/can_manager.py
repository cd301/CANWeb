"""
CANWeb – python-can bus manager.

Wraps python-can Bus objects and provides thread-safe access for the
Flask application.  A virtual (socketcan-based) bus is used when no real
hardware is available so the app can run without any dongle attached.
"""

from __future__ import annotations

import collections
import threading
import time
import random
import logging
from typing import Optional, Callable

import can


_log = logging.getLogger(__name__)

# Sliding window duration (seconds) used to calculate bus load.
_BUS_LOAD_WINDOW = 1.0


def _frame_bits(msg: can.Message) -> int:
    """Return the approximate number of bits a CAN frame occupies on the bus.

    For a standard (11-bit ID) data frame the overhead is 44 bits (SOF,
    arbitration, control, CRC, ACK, EOF, IFS).  Extended frames (29-bit ID)
    add 20 extra bits.  Each data byte is 8 bits plus up to 20 % bit-stuffing
    overhead is ignored here for simplicity – the result is a conservative
    lower-bound that matches common bus-load calculators.
    """
    overhead = 64 if getattr(msg, "is_extended_id", False) else 44
    return overhead + len(msg.data) * 8


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
        # Sliding-window bus load: deque of (timestamp, bits) tuples
        self._load_window: collections.deque = collections.deque()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # IDs that we simulate (realistic-ish)
    _ARBIT_IDS = [0x100, 0x200, 0x300, 0x400, 0x18FF1234, 0x18FF5378]

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
                self._load_window.append((time.monotonic(), _frame_bits(msg)))
                for cb in self._listeners:
                    try:
                        cb(msg)
                    except Exception:
                        _log.exception("CAN listener callback failed on simulated bus")
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
                    _log.exception("CAN listener callback failed during send")

    def shutdown(self):
        self._stop.set()

    def bus_load(self) -> float:
        now = time.monotonic()
        cutoff = now - _BUS_LOAD_WINDOW
        with self._lock:
            while self._load_window and self._load_window[0][0] < cutoff:
                self._load_window.popleft()
            window_bits = sum(bits for _, bits in self._load_window)
        capacity_bits = self.bitrate * _BUS_LOAD_WINDOW
        return min(100.0, window_bits / capacity_bits * 100)


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
        # Sliding-window bus load: deque of (timestamp, bits) tuples
        self._load_window: collections.deque = collections.deque()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                msg = self._bus.recv(timeout=0.1)
            except Exception:
                _log.exception("CAN receive loop failed")
                time.sleep(0.05)
                continue
            if msg is None:
                continue
            with self._lock:
                if msg.is_error_frame:
                    self.error_count += 1
                else:
                    self.rx_count += 1
                    self._load_window.append((time.monotonic(), _frame_bits(msg)))
                for cb in self._listeners:
                    try:
                        cb(msg)
                    except Exception:
                        _log.exception("CAN listener callback failed on real bus")

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
        now = time.monotonic()
        cutoff = now - _BUS_LOAD_WINDOW
        with self._lock:
            while self._load_window and self._load_window[0][0] < cutoff:
                self._load_window.popleft()
            window_bits = sum(bits for _, bits in self._load_window)
        capacity_bits = self.bitrate * _BUS_LOAD_WINDOW
        return min(100.0, window_bits / capacity_bits * 100)


# ──────────────────────────────────────────────────────────────────────────────
# Public manager
# ──────────────────────────────────────────────────────────────────────────────

_INTERFACE_HINTS: dict[str, str] = {
    "pcan": (
        " — ensure the PCAN driver (PEAK PCAN Basic) is installed and the device "
        "is plugged in. Check the channel name (e.g. PCAN_USBBUS1)."
    ),
    "kvaser": (
        " — ensure Kvaser drivers (canlib) are installed and the device is "
        "connected. Check the channel index (e.g. 0)."
    ),
    "socketcan": (
        " — ensure the SocketCAN interface is up (`ip link set <iface> up type can`)."
    ),
    "vector": (
        " — ensure Vector XL Driver Library is installed and an application "
        "channel is configured in Vector Hardware Config."
    ),
    "ixxat": (
        " — ensure the IXXAT VCI driver is installed and the device is connected."
    ),
    "serial": (
        " — ensure the serial port exists and is not in use by another process."
    ),
}


def _connection_hint(interface: str, channel: str) -> str:
    """Return a human-readable diagnostic hint for common CAN interfaces."""
    return _INTERFACE_HINTS.get(interface.lower(), "")


_bus_instance = None
_bus_lock = threading.Lock()


def get_bus():
    return _bus_instance


def connect(interface: str = "virtual", channel: str = "virtual",
            bitrate: int = 500_000, **kwargs):
    global _bus_instance
    with _bus_lock:
        if _bus_instance is not None:
            _bus_instance.shutdown()
        if interface == "virtual" or interface == "sim":
            _bus_instance = _SimulatedBus()
        else:
            try:
                _bus_instance = _RealBus(interface, channel, bitrate, **kwargs)
            except Exception as exc:
                _bus_instance = None
                hint = _connection_hint(interface, channel)
                raise RuntimeError(
                    f"Cannot open {interface} on channel '{channel}': {exc}{hint}"
                ) from exc
    return _bus_instance


def disconnect():
    global _bus_instance
    with _bus_lock:
        if _bus_instance is not None:
            _bus_instance.shutdown()
            _bus_instance = None


# Start simulated bus by default on import so the UI works immediately.
connect("sim")
