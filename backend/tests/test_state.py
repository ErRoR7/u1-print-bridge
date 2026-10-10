"""State machine + restart recovery tests on an in-memory DB."""
import json

import pytest

from backend import db, workers


@pytest.fixture()
def conn():
    c = db.open_db(":memory:")
    yield c
    c.close()


def _mk(conn, job_id="j1", status="created"):
    db.create_job(conn, job_id, title="t", source_url=None, source_type="upload", status=status)
    return job_id


def test_full_valid_chain(conn):
    jid = _mk(conn)
    for nxt in ("fetching", "inspecting", "awaiting_confirm", "slicing",
                "uploading", "printing", "done"):
        db.set_status(conn, jid, nxt)
    assert db.get_job(conn, jid)["status"] == "done"


def test_failed_from_any_non_terminal(conn):
    for start in ("created", "fetching", "inspecting", "awaiting_confirm",
                  "slicing", "uploading", "printing"):
        jid = _mk(conn, job_id=f"f-{start}", status=start)
        db.set_status(conn, jid, "failed")
        assert db.get_job(conn, jid)["status"] == "failed"


def test_invalid_transitions(conn):
    jid = _mk(conn)
    with pytest.raises(db.InvalidTransitionError):
        db.set_status(conn, jid, "done")  # created -> done is not allowed
    with pytest.raises(db.InvalidTransitionError):
        db.set_status(conn, jid, "printing")


def test_terminal_states_are_sticky(conn):
    jid = _mk(conn, job_id="t1", status="done")
    with pytest.raises(db.InvalidTransitionError):
        db.set_status(conn, jid, "created")


def test_claim_is_atomic(conn):
    jid = _mk(conn)
    assert db.claim(conn, jid, "slicer") is True
    assert db.claim(conn, jid, "slicer") is False  # already claimed
    db.release_claim(conn, jid)
    assert db.claim(conn, jid, "slicer", expect_status="created") is True


def test_log_capped_at_300(conn):
    jid = _mk(conn)
    for i in range(350):
        db.append_log(conn, jid, "info", f"msg {i}")
    log = json.loads(db.get_job(conn, jid)["log_json"])
    assert len(log) == db.LOG_CAP == 300
    assert log[0]["msg"] == "msg 50"  # oldest entries dropped


def test_restart_recovery(conn):
    _mk(conn, "a", status="fetching")
    _mk(conn, "b", status="slicing")
    _mk(conn, "c", status="printing")
    _mk(conn, "d", status="created")
    _mk(conn, "e", status="awaiting_confirm")
    db.claim(conn, "b", "slicer")

    summary = workers.restart_recovery(conn)

    assert set(summary["failed"]) == {"a", "b"}
    assert summary["kept_printing"] == ["c"]
    assert summary["requeued"] == ["d"]

    a = db.get_job(conn, "a")
    assert a["status"] == "failed"
    assert a["error_code"] == "restarted"
    assert "safe to retry" in a["error"]

    b = db.get_job(conn, "b")
    assert b["status"] == "failed" and b["claimed_by"] is None

    assert db.get_job(conn, "c")["status"] == "printing"  # kept for re-attach
    assert db.get_job(conn, "d")["status"] == "created"  # re-queued
    assert db.get_job(conn, "e")["status"] == "awaiting_confirm"  # stable


def test_open_db_migrates_old_schema(tmp_path):
    """A pre-existing DB without the new columns gains them on open."""
    import sqlite3

    p = str(tmp_path / "old.db")
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE jobs(id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "source_url TEXT, source_type TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'created', "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.commit()
    conn.close()
    conn = db.open_db(p)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"dimensions_json", "scale_json", "copies"} <= cols
    conn.close()
    # and a fresh DB carries the columns with defaults
    conn = db.open_db(":memory:")
    db.create_job(conn, "j9", title="t", source_url=None, source_type="upload")
    row = db.get_job(conn, "j9")
    assert row["copies"] == 1
    assert json.loads(row["scale_json"]) == {"x": 1.0, "y": 1.0, "z": 1.0}
    conn.close()
