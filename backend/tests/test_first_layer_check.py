"""Tests for the v1.0.18 first-layer check monitor helper (backend/workers.py).

_maybe_first_layer_check is extracted from the _poll_print loop so it can be
tested without running the polling thread.
"""
import json
from pathlib import Path

import pytest

from backend import db, workers
from backend.config import config


@pytest.fixture()
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "NTFY_TOPIC", "u1-print-bridge")
    monkeypatch.setattr(config, "BRIDGE_URL", "")
    conn = db.open_db(str(tmp_path / "jobs.db"))
    db.create_job(conn, "job1", title="Owl", source_url=None, source_type="upload",
                  status="printing")
    db.update_job(conn, "job1", first_layer_check=1)
    (tmp_path / "jobs" / "job1").mkdir(parents=True, exist_ok=True)
    yield conn, str(tmp_path / "jobs.db"), "job1"
    conn.close()


def _row(conn):
    return db.get_job(conn, "job1")


def test_check_fires_on_first_layer_pause(job, monkeypatch, tmp_path):
    conn, db_path, job_id = job
    monkeypatch.setattr(workers, "_camera_snapshot", lambda *a, **k: b"\xff\xd8fakejpeg")
    sent = []
    monkeypatch.setattr(workers.ntfy, "notify",
                        lambda *a, **k: sent.append((a, k)) or True)
    workers._maybe_first_layer_check(
        conn, db_path, job_id, _row(conn),
        {"state": "paused", "info": {"current_layer": 1}})
    row = _row(conn)
    assert row["first_layer_checked"] == 1
    assert row["first_layer_pending"] == 1
    assert row["first_layer_snapshot_path"]
    assert Path(row["first_layer_snapshot_path"]).read_bytes() == b"\xff\xd8fakejpeg"
    assert len(sent) == 1
    assert sent[0][0][2] == "first_layer_check"  # (server, topic, event, ...)
    assert any("First layer done" in e["msg"] for e in json.loads(row["log_json"]))


def test_check_fires_without_layer_info(job, monkeypatch):
    conn, db_path, job_id = job
    monkeypatch.setattr(workers, "_camera_snapshot", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(workers.ntfy, "notify",
                        lambda *a, **k: sent.append((a, k)) or True)
    workers._maybe_first_layer_check(conn, db_path, job_id, _row(conn),
                                     {"state": "paused"})
    row = _row(conn)
    assert row["first_layer_checked"] == 1
    assert row["first_layer_pending"] == 1
    assert row["first_layer_snapshot_path"] is None  # camera failed; still alerts
    assert len(sent) == 1


def test_check_skipped_when_past_layer_1(job, monkeypatch):
    conn, db_path, job_id = job
    sent = []
    monkeypatch.setattr(workers.ntfy, "notify",
                        lambda *a, **k: sent.append((a, k)) or True)
    workers._maybe_first_layer_check(
        conn, db_path, job_id, _row(conn),
        {"state": "paused", "info": {"current_layer": 2}})
    row = _row(conn)
    assert row["first_layer_checked"] == 1  # window passed; marked done
    assert row["first_layer_pending"] == 0
    assert sent == []  # no alert on a later layer
    assert any("window passed" in e["msg"] for e in json.loads(row["log_json"]))


def test_check_ignored_when_not_paused(job, monkeypatch):
    conn, db_path, job_id = job
    sent = []
    monkeypatch.setattr(workers.ntfy, "notify",
                        lambda *a, **k: sent.append((a, k)) or True)
    workers._maybe_first_layer_check(conn, db_path, job_id, _row(conn),
                                     {"state": "printing"})
    assert _row(conn)["first_layer_checked"] == 0
    assert sent == []


def test_check_ignored_when_disabled(job, monkeypatch):
    conn, db_path, job_id = job
    db.update_job(conn, "job1", first_layer_check=0)
    sent = []
    monkeypatch.setattr(workers.ntfy, "notify",
                        lambda *a, **k: sent.append((a, k)) or True)
    workers._maybe_first_layer_check(conn, db_path, job_id, _row(conn),
                                     {"state": "paused"})
    assert _row(conn)["first_layer_checked"] == 0
    assert sent == []


def test_check_fires_once_only(job, monkeypatch):
    conn, db_path, job_id = job
    monkeypatch.setattr(workers, "_camera_snapshot", lambda *a, **k: b"\xff\xd8x")
    sent = []
    monkeypatch.setattr(workers.ntfy, "notify",
                        lambda *a, **k: sent.append((a, k)) or True)
    ps = {"state": "paused", "info": {"current_layer": 1}}
    workers._maybe_first_layer_check(conn, db_path, job_id, _row(conn), ps)
    workers._maybe_first_layer_check(conn, db_path, job_id, _row(conn), ps)
    assert len(sent) == 1
