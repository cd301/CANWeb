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


def test_index_includes_plot_control_fields(client):
    rv = client.get("/")
    assert rv.status_code == 200
    assert b"plot-x-axis-title" in rv.data
    assert b"plot-y-axis-title" in rv.data
    assert b"X Axis Title:" in rv.data
    assert b"Y Axis Title:" in rv.data


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


def test_decode_message_truncated_data(client):
    """Decoding should succeed even when fewer bytes than DLC are received."""
    # default DBC EngineData is 8 bytes but we only send 2 bytes of data.
    # EngineSpeed occupies bits 0-15 so 2 bytes is enough to decode it.
    decoded = dbc_manager.decode_message(0x100, bytes([0x64, 0x00]))
    assert decoded is not None
    assert decoded["name"] == "EngineData"
    assert "EngineSpeed" in decoded["signals"]
    assert decoded["signals"]["EngineSpeed"] == pytest.approx(25.0)


DBC_WITH_CHOICES = b'''
VERSION ""

NS_ :

BS_:

BU_:

BO_ 512 StatusMsg: 1 Vector__XXX
 SG_ Status : 0|2@1+ (1,0) [0|3] "" Vector__XXX

VAL_ 512 Status 0 "OFF" 1 "ON" 2 "ERROR" 3 "UNKNOWN" ;

'''


DBC_WITH_EXTENDED_FRAME = b'''
VERSION ""

NS_ :
\tNS_DESC_
\tCM_
\tBA_DEF_
\tBA_
\tVAL_
\tCAT_DEF_
\tCAT_
\tFILTER
\tBA_DEF_DEF_
\tEV_DATA_
\tENVVAR_DATA_
\tSGTYPE_
\tSGTYPE_VAL_
\tBA_DEF_SGTYPE_
\tBA_SGTYPE_
\tSIG_TYPE_REF_
\tVAL_TABLE_
\tSIG_GROUP_
\tSIG_VALTYPE_
\tSIGTYPE_VALTYPE_
\tBO_TX_BU_
\tBA_DEF_REL_
\tBA_REL_
\tBA_DEF_DEF_REL_
\tBU_SG_REL_
\tBU_EV_REL_
\tBU_BO_REL_
\tSG_MUL_VAL_

BS_:

BU_: Host out

BO_ 2147483650 Direct: 8 out
 SG_ sig1 : 0|8@1+ (1,0) [0|0] "" Host
 SG_ skehk : 8|8@1+ (1,0) [0|0] "" Host
 SG_ asdfnlkw : 16|8@1+ (1,0) [0|0] "" Host
 SG_ sfdsaf : 24|8@1+ (1,0) [0|0] "" Host
 SG_ dfasutput : 32|8@1+ (1,0) [0|0] "" Host
 SG_ FanPWM : 40|8@1+ (1,0) [0|0] "" Host

'''


def test_decode_message_named_signal_value(client):
    """Signals with VAL_ definitions should decode to numeric float values."""
    import io
    client.post(
        "/api/dbc/upload",
        data={"file": (io.BytesIO(DBC_WITH_CHOICES), "choices.dbc")},
        content_type="multipart/form-data",
    )
    # Status=1 ("ON") should decode to 1.0 not raise
    decoded = dbc_manager.decode_message(0x200, bytes([0x01]))
    assert decoded is not None
    assert decoded["signals"]["Status"] == pytest.approx(1.0)


def test_dbc_upload_decodes_extended_frame_signals(client):
    rv = client.post(
        "/api/dbc/upload",
        data={"file": (io.BytesIO(DBC_WITH_EXTENDED_FRAME), "extended.dbc")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 200
    msg = rv.get_json()["messages"][0]
    assert msg["id"] == "0x2"
    assert msg["name"] == "Direct"
    assert "FanPWM" in msg["signals"]

    decoded = dbc_manager.decode_message(0x2, bytes([1, 2, 3, 4, 5, 6]), is_extended=True)
    assert decoded is not None
    assert decoded["name"] == "Direct"
    assert decoded["signals"] == {
        "sig1": pytest.approx(1.0),
        "skehk": pytest.approx(2.0),
        "asdfnlkw": pytest.approx(3.0),
        "sfdsaf": pytest.approx(4.0),
        "dfasutput": pytest.approx(5.0),
        "FanPWM": pytest.approx(6.0),
    }


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


def test_dbc_upload_redecodes_buffered_extended_frames(client):
    client.post("/api/dbc/clear")
    client.post("/api/buffer/clear")

    client.post(
        "/api/transmit",
        json={"id": "0x2", "data": "010203040506", "extended": True},
    )

    before = client.get("/api/buffer/recent?limit=5").get_json()
    assert any(frame["id"] == "0x2" and frame["ext"] is True and "decoded" not in frame for frame in before)

    rv = client.post(
        "/api/dbc/upload",
        data={"file": (io.BytesIO(DBC_WITH_EXTENDED_FRAME), "extended.dbc")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 200

    after = client.get("/api/buffer/recent?limit=5").get_json()
    decoded_frame = next(frame for frame in after if frame["id"] == "0x2" and frame["ext"] is True and "decoded" in frame)
    assert decoded_frame["decoded"]["name"] == "Direct"
    assert decoded_frame["decoded"]["signals"]["FanPWM"] == pytest.approx(6.0)


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


DBC_SECOND = b'''
VERSION ""

NS_ :

BS_:

BU_:

BO_ 512 BrakeData: 8 Vector__XXX
 SG_ BrakePressure : 0|8@1+ (1,0) [0|255] "bar" Vector__XXX

'''


def test_stacking_multiple_dbcs_single_request(client):
    """Selecting multiple DBC files at once merges them all into one DB."""
    client.post("/api/dbc/clear")

    rv = client.post(
        "/api/dbc/upload",
        data={
            "file": [
                (io.BytesIO(DBC_CONTENT), "engine.dbc"),
                (io.BytesIO(DBC_SECOND), "brake.dbc"),
            ]
        },
        content_type="multipart/form-data",
    )
    assert rv.status_code == 200

    rv_info = client.get("/api/dbc/info")
    data = rv_info.get_json()
    assert data["loaded"] is True
    msg_names = [m["name"] for m in data["messages"]]
    assert "EngineData" in msg_names
    assert "BrakeData" in msg_names
    assert len(data["filenames"]) == 2

    decoded_engine = dbc_manager.decode_message(0x100, bytes([0x64, 0x00, 0x80, 0x00, 0, 0, 0, 0]))
    assert decoded_engine is not None
    assert decoded_engine["name"] == "EngineData"

    decoded_brake = dbc_manager.decode_message(0x200, bytes([0x7F, 0, 0, 0, 0, 0, 0, 0]))
    assert decoded_brake is not None
    assert decoded_brake["name"] == "BrakeData"


def test_stacking_with_append_param(client):
    """Uploading separate files with append=true stacks onto the existing DB."""
    client.post("/api/dbc/clear")

    client.post(
        "/api/dbc/upload",
        data={"file": (io.BytesIO(DBC_CONTENT), "engine.dbc")},
        content_type="multipart/form-data",
    )

    rv = client.post(
        "/api/dbc/upload",
        data={"file": (io.BytesIO(DBC_SECOND), "brake.dbc"), "append": "true"},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 200

    rv_info = client.get("/api/dbc/info")
    data = rv_info.get_json()
    msg_names = [m["name"] for m in data["messages"]]
    assert "EngineData" in msg_names
    assert "BrakeData" in msg_names
    assert len(data["filenames"]) == 2


def test_stacking_replace_when_append_false(client):
    """Uploading with append=false (default) should replace the existing DB."""
    client.post("/api/dbc/clear")

    client.post(
        "/api/dbc/upload",
        data={"file": (io.BytesIO(DBC_CONTENT), "engine.dbc")},
        content_type="multipart/form-data",
    )

    client.post(
        "/api/dbc/upload",
        data={"file": (io.BytesIO(DBC_SECOND), "brake.dbc")},
        content_type="multipart/form-data",
    )

    rv_info = client.get("/api/dbc/info")
    data = rv_info.get_json()
    msg_names = [m["name"] for m in data["messages"]]
    assert "BrakeData" in msg_names
    assert "EngineData" not in msg_names
    assert len(data["filenames"]) == 1


def test_dbc_info_returns_filenames(client):
    """get_db_info should return a 'filenames' list."""
    dbc_manager.load_dbc(dbc_manager.default_dbc_path())
    rv = client.get("/api/dbc/info")
    data = rv.get_json()
    assert data["loaded"] is True
    assert "filenames" in data
    assert isinstance(data["filenames"], list)
    assert len(data["filenames"]) >= 1
