"""
CANWeb – DBC / signal decoder.

Wraps cantools to decode CAN messages given a loaded DBC database.
"""

from __future__ import annotations

import threading
from typing import Optional

import cantools


_db: Optional[cantools.db.Database] = None
_db_lock = threading.Lock()
_db_filename: str = ""


def load_dbc(filepath: str) -> dict:
    global _db, _db_filename
    db = cantools.database.load_file(filepath)
    with _db_lock:
        _db = db
        _db_filename = filepath
    return {
        "messages": [
            {
                "id": f"0x{m.frame_id:X}",
                "name": m.name,
                "signals": [s.name for s in m.signals],
            }
            for m in db.messages
        ]
    }


def decode_message(arb_id: int, data: bytes) -> Optional[dict]:
    with _db_lock:
        if _db is None:
            return None
        try:
            msg = _db.get_message_by_frame_id(arb_id)
            decoded = msg.decode(data, decode_choices=False)
            return {"name": msg.name, "signals": {k: float(v) for k, v in decoded.items()}}
        except Exception:
            return None


def get_db_info() -> Optional[dict]:
    with _db_lock:
        if _db is None:
            return None
        return {
            "filename": _db_filename,
            "messages": [
                {
                    "id": f"0x{m.frame_id:X}",
                    "name": m.name,
                    "signals": [
                        {"name": s.name, "unit": s.unit or "", "min": s.minimum, "max": s.maximum}
                        for s in m.signals
                    ],
                }
                for m in _db.messages
            ],
        }


def clear_dbc():
    global _db, _db_filename
    with _db_lock:
        _db = None
        _db_filename = ""
