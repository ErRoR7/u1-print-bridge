"""The done snapshot must be captured while the print is still finishing
(~99%, bed at print height), not on "complete" after the end G-code drops it."""
import sqlite3
from pathlib import Path

from backend import db, workers
from backend.config import config


def _mk_printing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    db_path = str(tmp_path / "jobs.db")
    conn = db.open_db(db_path)
    db.create_job(conn, "j1", title="T", source_url="https://x", source_type="printables", status="printing")
    db.update_job(conn, "j1", timelapse=0)
    conn.close()
    (tmp_path / "jobs" / "j1").mkdir(parents=True, exist_ok=True)
    return db_path


def test_snapshot_captured_at_99_percent(tmp_path, monkeypatch):
    from backend import moonraker as mk
    db_path = _mk_printing(tmp_path, monkeypatch)
    snaps = {"n": 0}

    def fake_snapshot():
        snaps["n"] += 1
        return b"fake-jpeg"

    def fake_query(base_url, timeout=8.0):
        # first poll: 99.2% and still printing -> snapshot; then complete.
        if snaps["n"] == 0:
            return {"print_stats": {"state": "printing", "print_duration": 10},
                    "display_status": {"progress": 0.992},
                    "virtual_sdcard": {"progress": 0.992}}
        return {"print_stats": {"state": "complete", "print_duration": 20},
                "display_status": {"progress": 1.0},
                "virtual_sdcard": {"progress": 1.0}}

    monkeypatch.setattr(mk, "query", fake_query)
    monkeypatch.setattr(workers, "_camera_snapshot", fake_snapshot)
    monkeypatch.setattr(workers, "ntfy", type("N", (), {"notify": staticmethod(lambda *a, **k: None)}))
    monkeypatch.setattr(workers, "POLL_S", 0)

    conn = db.open_db(db_path)
    workers._poll_print(conn, db_path, "j1")
    conn.close()

    assert snaps["n"] == 1, "exactly one snapshot, taken at 99%"
    conn = db.open_db(db_path)
    row = db.get_job(conn, "j1")
    conn.close()
    assert row["status"] == "done"
    assert row["final_snapshot_path"]
    assert Path(row["final_snapshot_path"]).read_bytes() == b"fake-jpeg"


def test_snapshot_falls_back_on_complete(tmp_path, monkeypatch):
    from backend import moonraker as mk
    db_path = _mk_printing(tmp_path, monkeypatch)
    snaps = {"n": 0}

    def fake_snapshot():
        snaps["n"] += 1
        return b"fake-jpeg"

    polls = {"n": 0}

    def fake_query(base_url, timeout=8.0):
        # jump straight from 50% to complete: the early window was missed.
        polls["n"] += 1
        if polls["n"] == 1:
            return {"print_stats": {"state": "printing", "print_duration": 10},
                    "display_status": {"progress": 0.5},
                    "virtual_sdcard": {"progress": 0.5}}
        return {"print_stats": {"state": "complete", "print_duration": 20},
                "display_status": {"progress": 1.0},
                "virtual_sdcard": {"progress": 1.0}}

    monkeypatch.setattr(mk, "query", fake_query)
    monkeypatch.setattr(workers, "_camera_snapshot", fake_snapshot)
    monkeypatch.setattr(workers, "ntfy", type("N", (), {"notify": staticmethod(lambda *a, **k: None)}))
    monkeypatch.setattr(workers, "POLL_S", 0)

    conn = db.open_db(db_path)
    workers._poll_print(conn, db_path, "j1")
    conn.close()

    assert snaps["n"] == 1, "fallback snapshot taken on complete"
    conn = db.open_db(db_path)
    row = db.get_job(conn, "j1")
    conn.close()
    assert row["status"] == "done"
    assert row["final_snapshot_path"]
