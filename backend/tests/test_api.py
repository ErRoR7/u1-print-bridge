"""API tests with FastAPI TestClient. Integrations are monkeypatched;
no LAN access is used."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import db, moonraker, spoolman
from backend.app import app
from backend.config import config


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "NTFY_TOPIC", "")
    # Startup probes must not touch the network.
    monkeypatch.setattr(moonraker, "health", lambda *a, **k: {"ok": False, "latency_ms": None, "detail": "mocked"})
    monkeypatch.setattr(spoolman, "health", lambda *a, **k: {"ok": False, "latency_ms": None, "detail": "mocked"})
    from backend import ntfy as ntfy_mod
    monkeypatch.setattr(ntfy_mod, "health", lambda *a, **k: {"ok": True, "latency_ms": None, "detail": "mocked"})
    with TestClient(app) as c:
        yield c


def _db():
    return db.open_db(str(Path(config.DATA_DIR) / "jobs.db"))


def _mk_awaiting(c=None):
    c = c or _db()
    db.create_job(c, "job1", title="Owl", source_url="https://x", source_type="printables",
                  status="awaiting_confirm")
    palette = [{"slot": 0, "hex": "FF0000", "material": "PLA"},
               {"slot": 1, "hex": "0000FF", "material": "PLA"}]
    db.update_job(c, "job1", palette_json=json.dumps(palette), plates=1)
    return c


def test_health_shape(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["version"] == "1.0.16"
    assert "started_at" in body
    for name in ("moonraker", "spoolman", "ntfy"):
        svc = body["services"][name]
        assert set(svc) == {"ok", "latency_ms", "detail"}


def test_config_shape(client):
    body = client.get("/api/config").json()
    assert set(body) == {"spoolman_url", "printer_url", "ntfy_enabled", "version", "qualities"}


def test_create_job_bad_url_422(client):
    for bad in ("notaurl", "ftp://example.com/x.3mf", "", "   "):
        r = client.post("/api/jobs", json={"source_url": bad})
        assert r.status_code == 422, bad


def test_confirm_wrong_state_409(client):
    c = _db()
    db.create_job(c, "j-wrong", title="t", source_url=None, source_type="upload", status="created")
    r = client.post("/api/jobs/j-wrong/confirm", json={"slot_spools": {}, "slot_toolheads": {"0": 0, "1": 1}, "plate": 0})
    assert r.status_code == 409


def test_confirm_bad_plate_422(client):
    _mk_awaiting()
    r = client.post("/api/jobs/job1/confirm", json={"slot_spools": {}, "slot_toolheads": {"0": 0, "1": 1}, "plate": 3})
    assert r.status_code == 422


def test_confirm_unknown_slot_422(client):
    _mk_awaiting()
    r = client.post("/api/jobs/job1/confirm", json={"slot_spools": {"9": 1}, "slot_toolheads": {"0": 0, "1": 1}, "plate": 0})
    assert r.status_code == 422


def test_confirm_printer_busy_409(client, monkeypatch):
    _mk_awaiting()
    monkeypatch.setattr(moonraker, "preflight_state", lambda *a, **k: "printing")
    r = client.post("/api/jobs/job1/confirm", json={"slot_spools": {"0": 5}, "slot_toolheads": {"0": 0, "1": 1}, "plate": 0})
    assert r.status_code == 409
    assert r.json()["detail"] == "Printer is busy"
    assert r.json()["printer_state"] == "printing"


def test_confirm_printer_unreachable_409(client, monkeypatch):
    _mk_awaiting()

    def boom(*a, **k):
        raise moonraker.MoonrakerError("down")

    monkeypatch.setattr(moonraker, "preflight_state", boom)
    r = client.post("/api/jobs/job1/confirm", json={"slot_spools": {"0": 5}, "slot_toolheads": {"0": 0, "1": 1}, "plate": 0})
    assert r.status_code == 409
    assert r.json()["detail"] == "Printer unreachable; check it and retry"


def test_confirm_ok_moves_to_slicing(client, monkeypatch):
    _mk_awaiting()
    monkeypatch.setattr(moonraker, "preflight_state", lambda *a, **k: "standby")
    r = client.post("/api/jobs/job1/confirm", json={"slot_spools": {"0": 5}, "slot_toolheads": {"0": 0, "1": 1}, "plate": 0})
    assert r.status_code == 202
    job = r.json()["job"]
    assert job["status"] == "slicing"
    assert job["slot_spools"] == {"0": 5}
    assert job["plate"] == 0


def test_spools_passthrough_shape(client, monkeypatch):
    fake = [{"id": 7, "name": "Red PLA", "color_hex": "FF0000",
             "material": "PLA", "remaining_g": 812.5, "location": "T0"}]
    monkeypatch.setattr(spoolman, "get_spools", lambda *a, **k: fake)
    r = client.get("/api/spools")
    assert r.status_code == 200
    assert r.json() == {"spools": fake}


def test_spools_down_502(client, monkeypatch):
    def boom(*a, **k):
        raise spoolman.SpoolmanError("down")

    monkeypatch.setattr(spoolman, "get_spools", boom)
    r = client.get("/api/spools")
    assert r.status_code == 502
    assert "detail" in r.json()


def test_jobs_list_and_404(client):
    assert client.get("/api/jobs").json() == {"jobs": []}
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.get("/api/jobs/nope/thumbnail").status_code == 404


def test_confirm_accepts_null_slots(client, monkeypatch):
    _mk_awaiting()
    monkeypatch.setattr(moonraker, "preflight_state", lambda *a, **k: "standby")
    r = client.post("/api/jobs/job1/confirm",
                    json={"slot_spools": {"0": 5, "1": None}, "slot_toolheads": {"0": 0, "1": 1}, "plate": 0})
    assert r.status_code == 202
    assert r.json()["job"]["slot_spools"] == {"0": 5}


def _th_printer():
    return [
        {"toolhead": "T0", "color_hex": "#ff0000", "material": "PLA"},
        {"toolhead": "T1", "color_hex": "#0000ff", "material": "PLA"},
        {"toolhead": "T2", "color_hex": None, "material": None},
        {"toolhead": "T3", "color_hex": None, "material": None},
    ]


def _th_spools():
    return [
        {"id": 1, "name": "Red", "color_hex": "FF0000", "material": "PLA",
         "remaining_g": 500, "location": "shelf"},
        {"id": 2, "name": "Blue", "color_hex": "0000FF", "material": "PLA",
         "remaining_g": 250, "location": "shelf"},
    ]


def test_toolheads_printer_first(client, monkeypatch):
    monkeypatch.setattr(moonraker, "toolhead_filaments", lambda *a, **k: _th_printer())
    monkeypatch.setattr(spoolman, "get_spools", lambda *a, **k: _th_spools())
    r = client.get("/api/toolheads")
    assert r.status_code == 200
    th = {t["toolhead"]: t for t in r.json()["toolheads"]}
    assert len(th) == 4
    assert th["T0"]["source"] == "printer"
    assert th["T0"]["color_hex"] == "#ff0000"
    assert th["T0"]["spool"]["id"] == 1  # enriched by nearest-color match
    assert th["T1"]["spool"]["id"] == 2
    assert th["T2"]["source"] == "none"
    assert th["T2"]["spool"] is None


def test_toolheads_spoolman_location_fallback(client, monkeypatch):
    monkeypatch.setattr(moonraker, "toolhead_filaments", lambda *a, **k: None)
    spools = _th_spools()
    spools[0]["location"] = "T0"
    monkeypatch.setattr(spoolman, "get_spools", lambda *a, **k: spools)
    th = {t["toolhead"]: t for t in client.get("/api/toolheads").json()["toolheads"]}
    assert th["T0"]["source"] == "spoolman"
    assert th["T0"]["spool"]["id"] == 1
    assert th["T1"]["source"] == "none"


def test_toolheads_both_down_502(client, monkeypatch):
    monkeypatch.setattr(moonraker, "toolhead_filaments", lambda *a, **k: None)

    def boom(*a, **k):
        raise spoolman.SpoolmanError("down")

    monkeypatch.setattr(spoolman, "get_spools", boom)
    assert client.get("/api/toolheads").status_code == 502


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_toolhead_filaments_parses_rgba(monkeypatch):
    # Shape seen in the wild (PAXX issue #541): RRGGBBAA per extruder.
    payload = {"result": {"status": {"print_task_config": {
        "filament_color_rgba": ["1B5E20FF", "FFFFFFFF", "000000FF", "FFFFFFFF"],
        "filament_color_multi": [{"colors": ["FFFFFF"]}],
    }}}}
    monkeypatch.setattr("backend.moonraker.httpx.get", lambda *a, **k: _FakeResp(payload))
    got = moonraker.toolhead_filaments("http://x:7125")
    assert got is not None and len(got) == 4
    assert got[0] == {"toolhead": "T0", "color_hex": "#1b5e20", "material": None}
    assert got[1]["color_hex"] == "#ffffff"
    assert got[2]["color_hex"] == "#000000"


def test_toolhead_filaments_missing_object_returns_none(monkeypatch):
    payload = {"result": {"status": {}}}
    monkeypatch.setattr("backend.moonraker.httpx.get", lambda *a, **k: _FakeResp(payload))
    assert moonraker.toolhead_filaments("http://x:7125") is None


def _mk_awaiting_with_dims():
    c = _mk_awaiting()
    db.update_job(c, "job1", dimensions_json=json.dumps({"x": 100.0, "y": 100.0, "z": 50.0}))
    return c


def test_confirm_scale_and_copies_ok(client, monkeypatch):
    _mk_awaiting_with_dims()
    monkeypatch.setattr(moonraker, "preflight_state", lambda *a, **k: "standby")
    r = client.post("/api/jobs/job1/confirm", json={
        "slot_spools": {"0": 5}, "slot_toolheads": {"0": 0, "1": 1}, "plate": 0,
        "scale": {"x": 0.5, "y": 0.5, "z": 2.0}, "copies": 4})
    assert r.status_code == 202
    job = r.json()["job"]
    assert job["scale"] == {"x": 0.5, "y": 0.5, "z": 2.0}
    assert job["copies"] == 4
    assert job["dimensions"] == {"x": 100.0, "y": 100.0, "z": 50.0}


def test_confirm_scale_too_big_422(client, monkeypatch):
    _mk_awaiting_with_dims()
    monkeypatch.setattr(moonraker, "preflight_state", lambda *a, **k: "standby")
    r = client.post("/api/jobs/job1/confirm",
                    json={"slot_spools": {}, "slot_toolheads": {"0": 0, "1": 1}, "plate": 0, "scale": {"x": 3, "y": 1, "z": 1}})
    assert r.status_code == 422
    assert "270" in r.json()["detail"]


def test_confirm_too_many_copies_422(client, monkeypatch):
    c = _mk_awaiting()
    db.update_job(c, "job1", dimensions_json=json.dumps({"x": 200.0, "y": 200.0, "z": 50.0}))
    monkeypatch.setattr(moonraker, "preflight_state", lambda *a, **k: "standby")
    r = client.post("/api/jobs/job1/confirm",
                    json={"slot_spools": {}, "slot_toolheads": {"0": 0, "1": 1}, "plate": 0, "copies": 2})
    assert r.status_code == 422
    assert "fit on the bed" in r.json()["detail"]


def test_confirm_bad_scale_and_copies_422(client, monkeypatch):
    _mk_awaiting_with_dims()
    monkeypatch.setattr(moonraker, "preflight_state", lambda *a, **k: "standby")
    for body in ({"scale": {"x": 0.01, "y": 1, "z": 1}},       # below 10%
                 {"scale": {"x": 11, "y": 1, "z": 1}},          # above 1000%
                 {"scale": {"x": "big", "y": 1, "z": 1}},       # not a number
                 {"copies": 0}, {"copies": 26}, {"copies": "3"}):
        r = client.post("/api/jobs/job1/confirm",
                        json={"slot_spools": {}, "slot_toolheads": {"0": 0, "1": 1}, "plate": 0, **body})
        assert r.status_code == 422, body


def test_confirm_defaults_without_scale(client, monkeypatch):
    _mk_awaiting()
    monkeypatch.setattr(moonraker, "preflight_state", lambda *a, **k: "standby")
    r = client.post("/api/jobs/job1/confirm", json={"slot_spools": {}, "slot_toolheads": {"0": 0, "1": 1}, "plate": 0})
    assert r.status_code == 202
    job = r.json()["job"]
    assert job["scale"] == {"x": 1.0, "y": 1.0, "z": 1.0}
    assert job["copies"] == 1
    assert job["dimensions"] is None


def test_camera_endpoint_ok(client, monkeypatch):
    payload = {"result": {"webcams": [
        {"name": "case", "enabled": True, "stream_url": "/webcam/webrtc",
         "snapshot_url": "/webcam/snapshot.jpg"},
    ]}}
    monkeypatch.setattr("backend.moonraker.httpx.get", lambda *a, **k: _FakeResp(payload))
    r = client.get("/api/camera")
    assert r.status_code == 200
    d = r.json()
    assert d["available"] is True
    assert d["name"] == "case"
    # camera is on port 80, not Moonraker's :7125
    assert d["stream_url"] == "http://192.168.68.242/webcam/stream.mjpg"
    assert d["snapshot_url"] == "http://192.168.68.242/webcam/snapshot.jpg"
    assert ":7125" not in d["stream_url"]


def test_camera_unavailable(client, monkeypatch):
    def boom(*a, **k):
        raise moonraker.MoonrakerError("down")

    monkeypatch.setattr(moonraker, "webcams", boom)
    r = client.get("/api/camera")
    assert r.status_code == 200
    assert r.json()["available"] is False


def test_camera_no_webcams(client, monkeypatch):
    monkeypatch.setattr("backend.moonraker.httpx.get",
                        lambda *a, **k: _FakeResp({"result": {"webcams": []}}))
    assert client.get("/api/camera").json()["available"] is False


def test_printables_files_ok(client, monkeypatch):
    from backend import fetchers
    monkeypatch.setattr(
        fetchers, "list_printables_files",
        lambda url: ("Cool Model", [{"id": "1", "name": "a.3mf", "kind": "3mf"},
                                    {"id": "2", "name": "b.stl", "kind": "stl"}]))
    r = client.post("/api/printables/files",
                    json={"url": "https://www.printables.com/model/123-x"})
    assert r.status_code == 200
    assert r.json()["title"] == "Cool Model"
    assert len(r.json()["files"]) == 2


def test_printables_files_bad_url(client):
    r = client.post("/api/printables/files", json={"url": "https://example.com/"})
    assert r.status_code == 422


def test_printables_files_fetch_error(client, monkeypatch):
    from backend import fetchers
    def boom(url):
        raise fetchers.FetchError("down")
    monkeypatch.setattr(fetchers, "list_printables_files", boom)
    r = client.post("/api/printables/files",
                    json={"url": "https://www.printables.com/model/123-x"})
    assert r.status_code == 502


def test_create_job_with_file_id(client):
    r = client.post("/api/jobs", json={"source_url": "https://www.printables.com/model/123-x",
                                       "file_id": "222"})
    assert r.status_code == 201
    c = _db()
    assert db.get_job(c, r.json()["job"]["id"])["file_id"] == "222"


def test_confirm_quality_and_timelapse(client, monkeypatch):
    _mk_awaiting()
    monkeypatch.setattr(moonraker, "preflight_state", lambda *a, **k: "standby")
    r = client.post("/api/jobs/job1/confirm",
                    json={"slot_spools": {}, "slot_toolheads": {"0": 0, "1": 1}, "plate": 0,
                          "quality": "fine", "timelapse": True})
    assert r.status_code == 202
    job = r.json()["job"]
    assert job["quality"] == "fine"
    assert job["timelapse"] is True


def test_confirm_bad_quality_422(client, monkeypatch):
    _mk_awaiting()
    monkeypatch.setattr(moonraker, "preflight_state", lambda *a, **k: "standby")
    r = client.post("/api/jobs/job1/confirm",
                    json={"slot_spools": {}, "slot_toolheads": {"0": 0, "1": 1}, "plate": 0, "quality": "ultra"})
    assert r.status_code == 422


def test_config_exposes_qualities(client):
    q = client.get("/api/config").json()["qualities"]
    assert set(q) == {"draft", "standard", "fine", "extrafine"}


def test_confirm_supports_modes(client, monkeypatch):
    _mk_awaiting()
    monkeypatch.setattr(moonraker, "preflight_state", lambda *a, **k: "standby")
    r = client.post("/api/jobs/job1/confirm",
                    json={"slot_spools": {}, "slot_toolheads": {"0": 0, "1": 1}, "plate": 0, "supports": "off"})
    assert r.status_code == 202
    assert r.json()["job"]["supports"] == "off"


def test_confirm_bad_supports_422(client, monkeypatch):
    _mk_awaiting()
    monkeypatch.setattr(moonraker, "preflight_state", lambda *a, **k: "standby")
    r = client.post("/api/jobs/job1/confirm",
                    json={"slot_spools": {}, "slot_toolheads": {"0": 0, "1": 1}, "plate": 0, "supports": "sometimes"})
    assert r.status_code == 422


def test_supports_mode_column_migrates(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    c = sqlite3.connect(str(p))
    c.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY)")
    c.commit()
    c.close()
    conn = db.open_db(str(p))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert "supports_mode" in cols
    conn.close()


def _mk_done_upload(tmp_path):
    c = _db()
    db.create_job(c, "old1", title="Old upload", source_url=None, source_type="upload", status="done")
    jdir = Path(config.DATA_DIR) / "jobs" / "old1"
    jdir.mkdir(parents=True, exist_ok=True)
    f = jdir / "model.3mf"
    f.write_bytes(b"PK-fake-3mf")
    db.update_job(c, "old1", input_path=str(f))
    return c


def test_reprint_upload_copies_file(client, monkeypatch):
    from backend import workers as workers_mod
    monkeypatch.setattr(workers_mod, "rerun_inspect", lambda *a, **k: None)
    _mk_done_upload(Path(config.DATA_DIR))
    r = client.post("/api/jobs/old1/reprint")
    assert r.status_code == 201, r.text
    job = r.json()["job"]
    assert job["id"] != "old1"
    assert job["status"] == "inspecting"
    assert job["title"] == "Old upload"
    row = db.get_job(_db(), job["id"])
    assert Path(row["input_path"]).is_file()
    assert Path(row["input_path"]).read_bytes() == b"PK-fake-3mf"


def test_reprint_url_preserves_file_id(client, monkeypatch):
    from backend import workers as workers_mod
    monkeypatch.setattr(workers_mod, "spawn_pipeline", lambda *a, **k: None)
    c = _db()
    db.create_job(c, "old2", title="Old URL", source_url="https://www.printables.com/model/1/x",
                  source_type="printables", status="failed")
    db.update_job(c, "old2", file_id="7784270")
    r = client.post("/api/jobs/old2/reprint")
    assert r.status_code == 201, r.text
    job = r.json()["job"]
    assert job["status"] == "created"  # pipeline is mocked, so it stays at created
    row = db.get_job(_db(), job["id"])
    assert row["file_id"] == "7784270"
    assert job["source_url"] == "https://www.printables.com/model/1/x"


def test_reprint_missing_source_409(client):
    c = _db()
    db.create_job(c, "old3", title="Gone", source_url=None, source_type="upload", status="cancelled")
    r = client.post("/api/jobs/old3/reprint")
    assert r.status_code == 409


def test_reprint_unknown_job_404(client):
    r = client.post("/api/jobs/nope/reprint")
    assert r.status_code == 404


def test_color_preview_renders_png(client):
    import numpy as np
    c = _db()
    db.create_job(c, "pv1", title="PV", source_url="https://x",
                  source_type="printables", status="awaiting_confirm")
    jdir = Path(config.DATA_DIR) / "jobs" / "pv1"
    jdir.mkdir(parents=True, exist_ok=True)
    xyz = np.array([[0, 0, 0], [10, 0, 0], [0, 10, 5]], dtype=np.float32)
    np.savez_compressed(str(jdir / "preview_points.npz"), xyz=xyz,
                        tool=np.array([0, 0, 1], dtype=np.uint8))
    r = client.get("/api/jobs/pv1/color-preview", params={"colors": "0:FF0000,1:0000FF"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_color_preview_404_without_data(client):
    c = _db()
    db.create_job(c, "pv2", title="PV2", source_url="https://x",
                  source_type="printables", status="awaiting_confirm")
    r = client.get("/api/jobs/pv2/color-preview", params={"colors": "0:FF0000"})
    assert r.status_code == 404


def test_color_preview_generates_points_on_demand(client, tmp_path):
    import numpy as np
    c = _db()
    db.create_job(c, "pv3", title="PV3", source_url="https://x",
                  source_type="printables", status="awaiting_confirm")
    g = Path(config.DATA_DIR) / "jobs" / "pv3" / "t.gcode"
    g.parent.mkdir(parents=True, exist_ok=True)
    lines = ["T0", "G92 E0"]
    e = 0.0
    for x in range(0, 20):
        e += 1.0
        lines.append(f"G1 X{x} Y0 Z0.2 E{e:.1f}")
    lines += ["; total estimated time: 10m 0s"]
    g.write_text("\n".join(lines) + "\n")
    c.execute("UPDATE jobs SET gcode_path=? WHERE id=?", (str(g), "pv3"))
    c.commit()
    r = client.get("/api/jobs/pv3/color-preview", params={"colors": "0:00FF00"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert (g.parent / "preview_points.npz").is_file()


def test_backfill_time_estimate_on_job_detail(client):
    c = _db()
    db.create_job(c, "pv4", title="PV4", source_url="https://x",
                  source_type="printables", status="awaiting_confirm")
    c.execute("UPDATE jobs SET estimates_json=? WHERE id=?",
              ('{"time_s": null, "filament_g": 1.0, "filament_g_by_tool": null}', "pv4"))
    g = Path(config.DATA_DIR) / "jobs" / "pv4" / "t.gcode"
    g.parent.mkdir(parents=True, exist_ok=True)
    g.write_text("; generated by OrcaSlicer\n; total estimated time: 1h 2m 3s\n")
    c.execute("UPDATE jobs SET gcode_path=? WHERE id=?", (str(g), "pv4"))
    c.commit()
    r = client.get("/api/jobs/pv4")
    assert r.status_code == 200
    assert r.json()["job"]["estimates"]["time_s"] == 3600 + 2 * 60 + 3


def test_toolheads_prefers_spoolman_location(client, monkeypatch):
    # Printer reports SWAPPED colors; Spoolman locations must win.
    monkeypatch.setattr(moonraker, "toolhead_filaments", lambda *a, **k: [
        {"toolhead": "T0", "color_hex": "#000000", "material": "PLA"},
        {"toolhead": "T1", "color_hex": "#ffffff", "material": "PLA"},
        {"toolhead": "T2", "color_hex": None, "material": None},
        {"toolhead": "T3", "color_hex": None, "material": None},
    ])
    monkeypatch.setattr(spoolman, "get_spools", lambda *a, **k: [
        {"id": 1, "color_hex": "#ffffff", "material": "PLA", "location": "T0",
         "name": "white", "remaining_g": 500},
        {"id": 2, "color_hex": "#000000", "material": "PLA", "location": "T1",
         "name": "black", "remaining_g": 500},
    ])
    r = client.get("/api/toolheads")
    assert r.status_code == 200
    th = {t["toolhead"]: t for t in r.json()["toolheads"]}
    assert th["T0"]["spool"]["id"] == 1
    assert th["T0"]["source"] == "spoolman"
    assert th["T0"]["mismatch"] is True  # printer says black, spoolman white
    assert th["T1"]["spool"]["id"] == 2
    assert th["T1"]["mismatch"] is True


def test_toolheads_printer_fallback_without_locations(client, monkeypatch):
    monkeypatch.setattr(moonraker, "toolhead_filaments", lambda *a, **k: [
        {"toolhead": "T0", "color_hex": "#ff0000", "material": "PLA"},
        {"toolhead": "T1", "color_hex": None, "material": None},
        {"toolhead": "T2", "color_hex": None, "material": None},
        {"toolhead": "T3", "color_hex": None, "material": None},
    ])
    monkeypatch.setattr(spoolman, "get_spools", lambda *a, **k: [
        {"id": 7, "color_hex": "#ff0000", "material": "PLA", "location": "",
         "name": "red", "remaining_g": 100},
    ])
    r = client.get("/api/toolheads")
    assert r.status_code == 200
    th = {t["toolhead"]: t for t in r.json()["toolheads"]}
    assert th["T0"]["spool"]["id"] == 7
    assert th["T0"]["source"] == "printer"
    assert th["T0"]["mismatch"] is False
