"""
CANWeb – python-can bus manager.

Wraps python-can Bus objects and provides thread-safe access for the
Flask application.  A virtual (socketcan-based) bus is used when no real
hardware is available so the app can run without any dongle attached.
"""

from __future__ import annotations

import threading
import time
import random
import queue
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


def connect(interface: str = "virtual", channel: str = "virtual",
            bitrate: int = 500_000, **kwargs):
    global _bus_instance
    with _bus_lock:
        if _bus_instance is not None:
            _bus_instance.shutdown()
        if interface == "virtual" or interface == "sim":
            _bus_instance = _SimulatedBus()
        else:
            _bus_instance = _RealBus(interface, channel, bitrate, **kwargs)
    return _bus_instance


def disconnect():
    global _bus_instance
    with _bus_lock:
        if _bus_instance is not None:
            _bus_instance.shutdown()
            _bus_instance = None


# Start simulated bus by default on import so the UI works immediately.
connect("sim")
