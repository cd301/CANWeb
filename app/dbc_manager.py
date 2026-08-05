"""
CANWeb – DBC / signal decoder.

Wraps cantools to decode CAN messages given a loaded DBC database.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import cantools


_log = logging.getLogger(__name__)


_db: Optional[cantools.db.Database] = None
_db_lock = threading.Lock()
_db_filename: str = ""


def _message_lookup_ids(arb_id: int, is_extended: Optional[bool] = None) -> list[int]:
    lookup_ids: list[int] = []
    if is_extended is not False:
        lookup_ids.append(0x80000000 | arb_id)
    lookup_ids.append(arb_id & 0x7FFFFFFF)
    if is_extended is not True and arb_id != (arb_id & 0x7FFFFFFF):
        lookup_ids.append(arb_id)
    return list(dict.fromkeys(lookup_ids))


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


def decode_message(arb_id: int, data: bytes, is_extended: Optional[bool] = None) -> Optional[dict]:
    with _db_lock:
        if _db is None:
            return None
        msg = None
        for lookup_id in _message_lookup_ids(arb_id, is_extended):
            try:
                msg = _db.get_message_by_frame_id(lookup_id)
                break
            except KeyError:
                continue
        if msg is None:
            return None
        try:
            decoded = msg.decode(data, decode_choices=False, allow_truncated=True)
            signals = {}
            for k, v in decoded.items():
                try:
                    signals[k] = float(v)
                except (TypeError, ValueError):
                    # Signal value is not numeric (e.g. NamedSignalValue from an
                    # AUTOSAR/ARXML DBC). Fall back to the raw integer if available,
                    # otherwise skip it so one bad signal doesn't drop the whole frame.
                    try:
                        signals[k] = float(v.value)
                    except Exception:
                        pass
            return {"name": msg.name, "signals": signals}
        except Exception:
            _log.exception("DBC decode failed for arbitration_id=0x%X", arb_id)
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


def default_dbc_path() -> str:
    return str((__import__("pathlib").Path(__file__).with_name("default.dbc")))


def clear_dbc():
    global _db, _db_filename
    with _db_lock:
        _db = None
        _db_filename = ""
