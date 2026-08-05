"""Basic API tests for CANWeb."""

import io
import json
import time
import pytest

# Patch can_manager before importing the app so no real CAN bus is needed.
import sys, types

# Import the app
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import app as flask_app, socketio
from app import dbc_manager


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    dbc_manager.load_dbc(dbc_manager.default_dbc_path())
    with flask_app.test_client() as c:
        yield c


def test_index(client):
    rv = client.get("/")
    assert rv.status_code == 200
    assert b"CANWeb" in rv.data


def test_dbc_info_no_dbc(client):
    client.post("/api/dbc/clear")
    rv = client.get("/api/dbc/info")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["loaded"] is False


DBC_CONTENT = b'''
VERSION ""

NS_ :

BS_:

BU_:

BO_ 256 EngineData: 8 Vector__XXX
 SG_ EngineSpeed : 0|16@1+ (0.25,0) [0|16383.75] "rpm" Vector__XXX
 SG_ Throttle : 16|8@1+ (0.392156863,0) [0|100] "%" Vector__XXX

'''


def test_default_dbc_loaded(client):
    rv = client.get("/api/dbc/info")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["loaded"] is True
    assert any(msg["name"] == "EngineData" for msg in data["messages"])


def test_decode_message_ignores_unknown_frame_id(client, caplog):
    caplog.set_level("ERROR")
    decoded = dbc_manager.decode_message(0x1A4, bytes.fromhex("0000000000000000"))
    assert decoded is None
    assert "DBC decode failed for arbitration_id=0x1A4" not in caplog.text


def test_dbc_upload_valid(client):
    rv = client.post(
        "/api/dbc/upload",
        data={"file": (io.BytesIO(DBC_CONTENT), "test.dbc")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["ok"] is True
    assert len(data["messages"]) == 1
    msg = data["messages"][0]
    assert msg["name"] == "EngineData"
    assert "EngineSpeed" in msg["signals"]
    assert "Throttle" in msg["signals"]


def test_dbc_upload_populates_info(client):
    client.post(
        "/api/dbc/upload",
        data={"file": (io.BytesIO(DBC_CONTENT), "test.dbc")},
        content_type="multipart/form-data",
    )
    rv = client.get("/api/dbc/info")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["loaded"] is True
    assert len(data["messages"]) == 1


def test_dbc_upload_invalid_file(client):
    rv = client.post(
        "/api/dbc/upload",
        data={"file": (io.BytesIO(b"not a dbc file"), "bad.dbc")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 400
    data = rv.get_json()
    assert data["ok"] is False


def test_dbc_clear(client):
    client.post(
        "/api/dbc/upload",
        data={"file": (io.BytesIO(DBC_CONTENT), "test.dbc")},
        content_type="multipart/form-data",
    )
    rv = client.post("/api/dbc/clear")
    assert rv.status_code == 200
    assert rv.get_json()["ok"] is True
    info_rv = client.get("/api/dbc/info")
    assert info_rv.get_json()["loaded"] is False


def test_buffer_recent(client):
    # Give simulator a moment to produce frames
    time.sleep(0.5)
    rv = client.get("/api/buffer/recent?limit=10")
    assert rv.status_code == 200
    frames = rv.get_json()
    assert isinstance(frames, list)


def test_signals_empty_without_dbc(client):
    rv = client.get("/api/signals")
    assert rv.status_code == 200
    assert isinstance(rv.get_json(), list)


def test_signals_includes_dbc_definitions(client):
    """After uploading a DBC, /api/signals should list DBC-defined signals
    even before any matching live frames have been received."""
    client.post(
        "/api/dbc/upload",
        data={"file": (io.BytesIO(DBC_CONTENT), "test.dbc")},
        content_type="multipart/form-data",
    )
    rv = client.get("/api/signals")
    assert rv.status_code == 200
    signals = rv.get_json()
    assert "EngineData.EngineSpeed" in signals
    assert "EngineData.Throttle" in signals
    # Clearing the DBC should remove the definitions from the list
    client.post("/api/dbc/clear")
    rv2 = client.get("/api/signals")
    signals2 = rv2.get_json()
    assert "EngineData.EngineSpeed" not in signals2


def test_dbc_upload_redecodes_buffered_frames(client):
    client.post("/api/dbc/clear")
    client.post("/api/buffer/clear")

    client.post(
        "/api/transmit",
        json={"id": "0x100", "data": "6400800000000000"},
    )

    before = client.get("/api/buffer/recent?limit=5").get_json()
    assert any(frame["id"] == "0x100" and "decoded" not in frame for frame in before)

    rv = client.post(
        "/api/dbc/upload",
        data={"file": (io.BytesIO(DBC_CONTENT), "test.dbc")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 200

    after = client.get("/api/buffer/recent?limit=5").get_json()
    decoded_frame = next(frame for frame in after if frame["id"] == "0x100" and "decoded" in frame)
    assert decoded_frame["decoded"]["name"] == "EngineData"
    assert decoded_frame["decoded"]["signals"]["EngineSpeed"] == pytest.approx(25.0)
    assert decoded_frame["decoded"]["signals"]["Throttle"] == pytest.approx(50.196078464)

    signal_points = client.get("/api/signals/EngineData.EngineSpeed").get_json()["points"]
    assert signal_points
    assert signal_points[-1][1] == pytest.approx(25.0)


def test_transmit_valid(client):
    rv = client.post(
        "/api/transmit",
        json={"id": "0x100", "data": "DEADBEEF"},
    )
    assert rv.status_code == 200
    assert rv.get_json()["ok"] is True


def test_transmit_bad_data(client):
    rv = client.post(
        "/api/transmit",
        json={"id": "0x100", "data": "ZZ"},  # invalid hex
    )
    assert rv.status_code == 400


def test_transmit_bad_data_logs_failure(client, caplog):
    caplog.set_level("ERROR")
    rv = client.post(
        "/api/transmit",
        json={"id": "0x100", "data": "ZZ"},
    )
    assert rv.status_code == 400
    assert "CAN transmit failed" in caplog.text


def test_export_csv(client):
    rv = client.get("/api/export/csv")
    assert rv.status_code == 200
    assert rv.content_type.startswith("text/csv")


def test_buffer_clear(client):
    rv = client.post("/api/buffer/clear")
    assert rv.status_code == 200
    assert rv.get_json()["ok"] is True


def test_config_save_json(client):
    rv = client.post(
        "/api/config/save",
        json={"format": "json", "config": {"interface": "sim", "plotTabs": []}},
    )
    assert rv.status_code == 200
    payload = json.loads(rv.data)
    assert payload["interface"] == "sim"


def test_config_save_yaml(client):
    import yaml
    rv = client.post(
        "/api/config/save",
        json={"format": "yaml", "config": {"interface": "sim"}},
    )
    assert rv.status_code == 200
    payload = yaml.safe_load(rv.data)
    assert payload["interface"] == "sim"


def test_config_load_json(client):
    cfg = json.dumps({"interface": "pcan", "plotTabs": []}).encode()
    rv = client.post(
        "/api/config/load",
        data={"file": (io.BytesIO(cfg), "config.json")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["ok"] is True
    assert data["config"]["interface"] == "pcan"


def test_config_load_invalid_logs_failure(client, caplog):
    caplog.set_level("ERROR")
    rv = client.post(
        "/api/config/load",
        data={"file": (io.BytesIO(b"{invalid"), "config.json")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 400
    assert "Config load failed: filename=config.json" in caplog.text


def test_connect_sim(client):
    rv = client.post(
        "/api/connect",
        json={"interface": "sim", "channel": "virtual", "bitrate": 500000},
    )
    assert rv.status_code == 200
    assert rv.get_json()["ok"] is True


def test_disconnect(client):
    rv = client.post("/api/disconnect")
    assert rv.status_code == 200
    assert rv.get_json()["ok"] is True
    # Reconnect for other tests
    client.post("/api/connect", json={"interface": "sim"})
