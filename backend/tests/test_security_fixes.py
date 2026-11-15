"""Regression tests for the v1.0.20 security review fixes.

Each test pins one finding so it cannot silently regress:
- H2: MakerWorld/Printables routing by parsed hostname, not substring.
- H4: SSRF guard on download URLs + optional bearer auth on the API.
- H3: confirm keeps the proposed spool mapping; filament profiles use
  the live spool snapshot even when the mapping is empty.
- M5: download size cap + redirect-hop SSRF check.
- M6: 3MF vertex parsing with reordered / extra attributes.
- M7: slicer releases stale 'slicing' claims on startup.
"""
import importlib.util
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import db, fetchers, moonraker, spoolman
from backend.app import app
from backend.config import config

WORKER = Path(__file__).resolve().parent.parent.parent / "slicer" / "worker.py"


@pytest.fixture()
def worker():
    spec = importlib.util.spec_from_file_location("slicer_worker", WORKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "NTFY_TOPIC", "")
    monkeypatch.setattr(moonraker, "health", lambda *a, **k: {"ok": False, "latency_ms": None, "detail": "mocked"})
    monkeypatch.setattr(spoolman, "health", lambda *a, **k: {"ok": False, "latency_ms": None, "detail": "mocked"})
    from backend import ntfy as ntfy_mod
    monkeypatch.setattr(ntfy_mod, "health", lambda *a, **k: {"ok": True, "latency_ms": None, "detail": "mocked"})
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------------ H2

def test_detect_source_type_uses_hostname_not_substring():
    # A URL that merely mentions makerworld.com must NOT route there:
    # the legacy downloader would send the session cookie to that host.
    assert fetchers.detect_source_type("https://attacker.tld/?makerworld.com") == "other"
    assert fetchers.detect_source_type("https://attacker.tld/?printables.com/model/123") == "other"
    assert fetchers.detect_source_type("https://notmakerworld.com/models/1") == "other"
    # Real hosts still route.
    assert fetchers.detect_source_type(
        "https://makerworld.com/en/models/1814598-x") == "makerworld"
    assert fetchers.detect_source_type(
        "https://www.makerworld.com/en/models/1814598-x") == "makerworld"
    assert fetchers.detect_source_type(
        "https://www.printables.com/model/1234-thing") == "printables"


# ------------------------------------------------------------------ H4 (SSRF)

def test_check_url_public_rejects_private_targets():
    for bad in (
        "http://192.168.68.1/admin",
        "http://10.0.0.5/x.3mf",
        "http://127.0.0.1:7125/printer/info",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/x",
        "ftp://8.8.8.8/x.3mf",
    ):
        with pytest.raises(fetchers.FetchError):
            fetchers._check_url_public(bad)


def test_check_url_public_accepts_public_ip():
    fetchers._check_url_public("http://8.8.8.8/model.3mf")  # no raise


class _FakeResp:
    def __init__(self, status_code=200, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise fetchers.httpx.HTTPStatusError("err", request=None, response=self)

    def iter_bytes(self, _chunk=65536):
        yield self._body


def _fake_stream_factory(responses, seen):
    def fake_stream(method, url, **kwargs):
        seen.append(url)
        resp = responses.pop(0)
        return resp
    return fake_stream


def test_download_stream_size_cap(tmp_path, monkeypatch):
    dest = tmp_path / "model.3mf"
    seen = []
    monkeypatch.setattr(
        fetchers.httpx, "stream",
        _fake_stream_factory([_FakeResp(200, body=b"x" * 100)], seen))
    # Public IP so the SSRF guard passes; tiny cap so it must trip.
    with pytest.raises(fetchers.FetchError, match="exceeds the"):
        fetchers._download_stream("http://8.8.8.8/model.3mf", dest, max_bytes=10)
    assert not dest.exists()


def test_download_stream_redirect_to_private_blocked(tmp_path, monkeypatch):
    dest = tmp_path / "model.3mf"
    seen = []
    monkeypatch.setattr(
        fetchers.httpx, "stream",
        _fake_stream_factory(
            [_FakeResp(302, {"location": "http://192.168.1.1/evil.3mf"})], seen))
    with pytest.raises(fetchers.FetchError, match="non-public"):
        fetchers._download_stream("http://8.8.8.8/start.3mf", dest)
    assert seen == ["http://8.8.8.8/start.3mf"]  # redirect target never fetched


def test_download_stream_follows_public_redirect(tmp_path, monkeypatch):
    dest = tmp_path / "model.3mf"
    seen = []
    monkeypatch.setattr(
        fetchers.httpx, "stream",
        _fake_stream_factory(
            [_FakeResp(302, {"location": "https://8.8.4.4/file.3mf"}),
             _FakeResp(200, body=b"PK\x03\x04model")], seen))
    fetchers._download_stream("http://8.8.8.8/start.3mf", dest)
    assert dest.read_bytes() == b"PK\x03\x04model"
    assert seen == ["http://8.8.8.8/start.3mf", "https://8.8.4.4/file.3mf"]


# ------------------------------------------------------------------ H4 (auth)

def test_api_auth_off_by_default(client):
    assert client.get("/api/jobs").status_code == 200


def test_api_auth_token_gates_routes(client, monkeypatch):
    monkeypatch.setattr(config, "BRIDGE_API_TOKEN", "sekrit")
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/health").status_code == 200  # health stays open
    assert client.get("/api/jobs",
                      headers={"Authorization": "Bearer sekrit"}).status_code == 200
    assert client.get("/api/jobs",
                      headers={"Authorization": "Bearer wrong"}).status_code == 401


# ------------------------------------------------------------------ H3

def _mk_awaiting_with_proposal():
    c = db.open_db(str(Path(config.DATA_DIR) / "jobs.db"))
    db.create_job(c, "job1", title="Owl", source_url="https://x",
                  source_type="printables", status="awaiting_confirm")
    palette = [{"slot": 0, "hex": "FF0000", "material": "PLA"},
               {"slot": 1, "hex": "0000FF", "material": "PLA"}]
    db.update_job(c, "job1", palette_json=json.dumps(palette), plates=1,
                  slot_spools_json=json.dumps({"0": 5}))
    return c


def test_confirm_keeps_proposed_slot_spools(client, monkeypatch):
    _mk_awaiting_with_proposal()
    monkeypatch.setattr(moonraker, "preflight_state", lambda *a, **k: "standby")
    monkeypatch.setattr(moonraker, "toolhead_filaments", lambda *a, **k: [])
    r = client.post("/api/jobs/job1/confirm",
                    json={"slot_spools": {}, "slot_toolheads": {"0": 0, "1": 1}, "plate": 0})
    assert r.status_code == 202
    # The empty mapping from the frontend must NOT wipe the proposal:
    # the filament/material checks read this mapping.
    assert r.json()["job"]["slot_spools"] == {"0": 5}


def test_build_filament_profiles_uses_live_spool_without_mapping(worker, tmp_path, monkeypatch):
    tpl = {"name": "Snapmaker PETG", "filament_settings_id": "SM_PETG",
           "filament_type": "PETG", "filament_colour": ["#FFFFFF"]}
    (tmp_path / "filament_PETG.json").write_text(json.dumps(tpl))
    (tmp_path / "filament_PLA.json").write_text(json.dumps(
        {"name": "Snapmaker PLA", "filament_settings_id": "SM_PLA",
         "filament_type": "PLA", "filament_colour": ["#FFFFFF"]}))
    monkeypatch.setattr(worker, "FILAMENT_DIR", tmp_path)
    plan = [{"slot": 0, "required_hex": "00FF00", "required_material": "PLA",
             "spool": {"id": 7, "color_hex": "E72F1D", "material": "PETG"}}]
    paths = worker.build_filament_profiles(tmp_path, plan, {})  # empty mapping
    f0 = json.loads(Path(paths[0]).read_text())
    assert f0["filament_colour"] == ["#E72F1D"]  # live spool wins, not required hex
    assert f0["filament_settings_id"] == "SM_PETG_T0"  # live material wins


# ------------------------------------------------------------------ M6

def _make_reordered_3mf(path, wx=10.0, wy=20.0, wz=30.0):
    """Box 3MF whose vertices list z, y, x (reordered) plus an extra attr.

    The old regex required x=".." y=".." z=".." in exact order and would
    silently find zero vertices here.
    """
    verts = "".join(
        '<vertex z="%s" y="%s" x="%s" extra="1"/>\n' % (z, y, x)
        for x in (0, wx) for y in (0, wy) for z in (0, wz))
    obj = ('<object id="2" type="model"><mesh><vertices>%s</vertices>'
           '<triangles><triangle v1="4" v2="5" v3="6"/></triangles></mesh></object>' % verts)
    # Triangle (4,5,6) does not pass through the origin, so the signed
    # volume is non-zero (1.0 cm^3) -- a triangle through (0,0,0) would
    # give 0.0 and prove nothing about the parse.
    model = ('<?xml version="1.0"?><model unit="millimeter"><resources>%s</resources>'
             '<build><item objectid="2"/></build></model>' % obj)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("3D/3dmodel.model", model)
    return path


def test_threedmf_reordered_vertex_attributes(tmp_path):
    from backend import threedmf
    path = _make_reordered_3mf(tmp_path / "box.3mf")
    insp = threedmf.inspect(path)
    assert insp.dimensions == {"x": 10.0, "y": 20.0, "z": 30.0}
    vols = threedmf.estimate_volumes(path)
    assert vols.get(1, 0) > 0


def test_scale_3mf_reordered_vertex_attributes(worker, tmp_path):
    src = _make_reordered_3mf(tmp_path / "box.3mf")
    dst = tmp_path / "scaled.3mf"
    worker.scale_3mf(src, dst, 2.0, 0.5, 1.0)
    data = zipfile.ZipFile(dst).read("3D/3dmodel.model").decode()
    import re as _re
    xs = [float(v) for v in _re.findall(r'x="([\d.\-]+)"', data)]
    ys = [float(v) for v in _re.findall(r'y="([\d.\-]+)"', data)]
    zs = [float(v) for v in _re.findall(r'z="([\d.\-]+)"', data)]
    assert (round(max(xs) - min(xs), 1), round(max(ys) - min(ys), 1),
            round(max(zs) - min(zs), 1)) == (20.0, 10.0, 30.0)
    # Scaled output is still a valid model the inspector can read.
    from backend import threedmf
    assert threedmf.inspect(dst).dimensions == {"x": 20.0, "y": 10.0, "z": 30.0}


# ------------------------------------------------------------------ M7

def test_release_stale_slicing_claims(worker, tmp_path, monkeypatch):
    db_path = tmp_path / "jobs.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT, "
                 "claimed_by TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('stale', 'slicing', 'slicer-999', 'x')")
    conn.execute("INSERT INTO jobs VALUES ('api-claim', 'slicing', 'api-1', 'x')")
    conn.execute("INSERT INTO jobs VALUES ('printing', 'printing', 'slicer-999', 'x')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(worker, "DB_PATH", db_path)
    worker._release_stale_slicing_claims()
    conn = sqlite3.connect(db_path)
    rows = {r[0]: (r[1], r[2]) for r in
            conn.execute("SELECT id, status, claimed_by FROM jobs")}
    assert rows["stale"] == ("slicing", None)  # released, re-sliceable
    assert rows["api-claim"] == ("slicing", "api-1")  # not a slicer claim
    assert rows["printing"] == ("printing", "slicer-999")  # untouched
    conn.close()
