"""Tests for the Bambu -> U1 converter sidecar integration (backend/converter.py
and the workers._maybe_convert hook).

Detection is grounded in the converter's own rules (thadius83/bambu-to-
snapmaker-u1): a file is a Bambu project when Metadata/project_settings.config
declares printer_model starting with "Bambu Lab"; a file is already converted
when it declares a Snapmaker printer.
"""
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from backend import converter, db, workers
from backend.config import config

FAKE_CONVERTED = b"PK\x03\x04converted-3mf-bytes"


def _make_3mf(path, *, printer_model=None, colours=("#FF0000FF", "#00FF00FF"),
              extruders=(1, 2)):
    """Minimal 3MF with a real box mesh + optional Bambu-style project settings."""
    model_settings = '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
    for i, e in enumerate(extruders):
        model_settings += (
            '  <object id="%d"><metadata key="extruder" value="%d"/></object>\n' % (i, e)
        )
    model_settings += "</config>\n"
    verts = "".join(
        '<vertex x="%s" y="%s" z="%s"/>\n' % (x, y, z)
        for x in (0, 10) for y in (0, 10) for z in (0, 10))
    obj = ('<object id="0" type="model"><mesh><vertices>%s</vertices>'
           '<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>' % verts)
    model = ('<?xml version="1.0"?><model unit="millimeter"><resources>%s</resources>'
             '<build><item objectid="0"/></build></model>' % obj)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("3D/3dmodel.model", model)
        if printer_model is not None:
            zf.writestr("Metadata/project_settings.config", json.dumps({
                "printer_model": printer_model,
                "filament_colour": list(colours),
                "filament_type": ["PLA"] * len(colours),
            }))
        zf.writestr("Metadata/model_settings.config", model_settings)
    return path


def test_is_bambu_3mf(tmp_path):
    bambu = _make_3mf(tmp_path / "bambu.3mf", printer_model="Bambu Lab X1 Carbon")
    assert converter.is_bambu_3mf(bambu) is True
    u1 = _make_3mf(tmp_path / "u1.3mf", printer_model="Snapmaker U1")
    assert converter.is_bambu_3mf(u1) is False
    bare = _make_3mf(tmp_path / "bare.3mf", printer_model=None)
    assert converter.is_bambu_3mf(bare) is False


def test_is_bambu_3mf_non_3mf_and_missing(tmp_path):
    stl = tmp_path / "cube.stl"
    stl.write_bytes(b"solid cube\nendsolid cube\n")
    assert converter.is_bambu_3mf(stl) is False
    assert converter.is_bambu_3mf(tmp_path / "nope.3mf") is False


def test_should_convert_bambu_four_colours(tmp_path):
    p = _make_3mf(tmp_path / "b.3mf", printer_model="Bambu Lab P1S",
                  colours=("#FF0000FF", "#00FF00FF", "#0000FFFF", "#FFFFFF"))
    ok, reason = converter.should_convert(p)
    assert ok is True
    assert "Bambu" in reason


def test_should_convert_rejects_more_than_four_colours(tmp_path):
    p = _make_3mf(tmp_path / "b.3mf", printer_model="Bambu Lab X1 Carbon",
                  colours=("#FF0000FF",) * 6, extruders=(1, 2, 3, 4, 5, 6))
    ok, reason = converter.should_convert(p)
    assert ok is False
    assert "6 filament colours" in reason


def test_should_convert_rejects_u1_and_bare(tmp_path):
    u1 = _make_3mf(tmp_path / "u1.3mf", printer_model="Snapmaker U1")
    ok, _ = converter.should_convert(u1)
    assert ok is False
    bare = _make_3mf(tmp_path / "bare.3mf", printer_model=None)
    ok, _ = converter.should_convert(bare)
    assert ok is False


def _mock_client(monkeypatch, handler):
    # Capture the real Client first: converter.httpx IS the global httpx
    # module, so patching Client must not recurse through the patch itself.
    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        converter.httpx, "Client",
        lambda **kw: real_client(transport=transport))


def _mock_get(monkeypatch, handler):
    """Patch httpx.get for converter.health: forward only url + timeout."""
    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        converter.httpx, "get",
        lambda url, **kw: real_client(transport=transport).get(
            url, timeout=kw.get("timeout")))


def test_convert_file_success(tmp_path, monkeypatch):
    src = _make_3mf(tmp_path / "b.3mf", printer_model="Bambu Lab X1 Carbon")
    dest = tmp_path / "out.3mf"
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/convert":
            seen["convert_body"] = request.content
            return httpx.Response(200, json={
                "job_id": "job123", "download_name": "b-U1.3mf",
                "diff": {"counts": {"keys_dropped": 12, "rules_matched": 2}}})
        if request.url.path == "/api/download/job123":
            return httpx.Response(200, content=FAKE_CONVERTED)
        return httpx.Response(404)

    _mock_client(monkeypatch, handler)
    counts = converter.convert_file("http://converter:8080", src, dest)
    assert counts == {"keys_dropped": 12, "rules_matched": 2}
    assert dest.read_bytes() == FAKE_CONVERTED
    assert b"0.20 Standard" in seen["convert_body"]


def test_convert_file_http_error(tmp_path, monkeypatch):
    src = _make_3mf(tmp_path / "b.3mf", printer_model="Bambu Lab X1 Carbon")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "conversion failed"})

    _mock_client(monkeypatch, handler)
    with pytest.raises(converter.ConverterError):
        converter.convert_file("http://converter:8080", src, tmp_path / "out.3mf")


def test_convert_file_connect_error(tmp_path, monkeypatch):
    src = _make_3mf(tmp_path / "b.3mf", printer_model="Bambu Lab X1 Carbon")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _mock_client(monkeypatch, handler)
    with pytest.raises(converter.ConverterError):
        converter.convert_file("http://converter:8080", src, tmp_path / "out.3mf")


def test_convert_file_missing_job_id(tmp_path, monkeypatch):
    src = _make_3mf(tmp_path / "b.3mf", printer_model="Bambu Lab X1 Carbon")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"diff": {}})

    _mock_client(monkeypatch, handler)
    with pytest.raises(converter.ConverterError):
        converter.convert_file("http://converter:8080", src, tmp_path / "out.3mf")


def test_health_disabled_when_unconfigured():
    assert converter.health("") == {
        "ok": True, "latency_ms": None, "detail": "not configured"}


def test_health_ok_and_down(monkeypatch):
    def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    _mock_get(monkeypatch, ok_handler)
    result = converter.health("http://converter:8080")
    assert result["ok"] is True

    def down_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _mock_get(monkeypatch, down_handler)
    result = converter.health("http://converter:8080")
    assert result["ok"] is False


# ------------------------------------------------------- workers hook

def _make_job(tmp_path, monkeypatch, input_path):
    """A real jobs DB row + job dir, with DATA_DIR pointed at tmp_path."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    conn = db.open_db(str(tmp_path / "jobs.db"))
    job_id = "job-test-1"
    db.create_job(conn, job_id, title="test", source_url=None, source_type="upload")
    db.update_job(conn, job_id, input_path=str(input_path))
    jdir = tmp_path / "jobs" / job_id
    jdir.mkdir(parents=True, exist_ok=True)
    return conn, job_id, jdir


def test_maybe_convert_success_updates_input_path(tmp_path, monkeypatch):
    src = _make_3mf(tmp_path / "b.3mf", printer_model="Bambu Lab X1 Carbon")
    conn, job_id, jdir = _make_job(tmp_path, monkeypatch, src)
    monkeypatch.setattr(config, "CONVERTER_URL", "http://converter:8080")

    def fake_convert(base_url, src_path, dest_path, timeout=120.0):
        Path(dest_path).write_bytes(FAKE_CONVERTED)
        return {"keys_dropped": 12}

    monkeypatch.setattr(converter, "convert_file", fake_convert)
    row = db.get_job(conn, job_id)
    result = workers._maybe_convert(conn, job_id, row, jdir)
    assert result == str(jdir / "model_converted.3mf")
    assert db.get_job(conn, job_id)["input_path"] == str(jdir / "model_converted.3mf")
    logs = db.get_job(conn, job_id)["log_json"]
    assert "Converted Bambu project" in logs
    assert "keys_dropped=12" in logs


def test_maybe_convert_failure_falls_back_to_original(tmp_path, monkeypatch):
    src = _make_3mf(tmp_path / "b.3mf", printer_model="Bambu Lab X1 Carbon")
    conn, job_id, jdir = _make_job(tmp_path, monkeypatch, src)
    monkeypatch.setattr(config, "CONVERTER_URL", "http://converter:8080")

    def fake_convert(*a, **kw):
        raise converter.ConverterError("ConnectError: refused")

    monkeypatch.setattr(converter, "convert_file", fake_convert)
    row = db.get_job(conn, job_id)
    result = workers._maybe_convert(conn, job_id, row, jdir)
    assert result == str(src)
    assert db.get_job(conn, job_id)["input_path"] == str(src)
    assert "converter unavailable" in db.get_job(conn, job_id)["log_json"].lower()


def test_maybe_convert_skipped_when_disabled(tmp_path, monkeypatch):
    src = _make_3mf(tmp_path / "b.3mf", printer_model="Bambu Lab X1 Carbon")
    conn, job_id, jdir = _make_job(tmp_path, monkeypatch, src)
    monkeypatch.setattr(config, "CONVERTER_URL", "")
    called = []
    monkeypatch.setattr(converter, "convert_file",
                        lambda *a, **kw: called.append(True))
    row = db.get_job(conn, job_id)
    assert workers._maybe_convert(conn, job_id, row, jdir) == str(src)
    assert called == []


def test_maybe_convert_skips_non_bambu(tmp_path, monkeypatch):
    src = _make_3mf(tmp_path / "u1.3mf", printer_model="Snapmaker U1")
    conn, job_id, jdir = _make_job(tmp_path, monkeypatch, src)
    monkeypatch.setattr(config, "CONVERTER_URL", "http://converter:8080")
    called = []
    monkeypatch.setattr(converter, "convert_file",
                        lambda *a, **kw: called.append(True))
    row = db.get_job(conn, job_id)
    assert workers._maybe_convert(conn, job_id, row, jdir) == str(src)
    assert called == []
