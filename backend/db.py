"""SQLite persistence for the U1 print bridge.

One table, WAL mode, shared between the backend container and the slicer
container via the DATA_DIR volume. The slicer worker only ever touches
status/claimed_by/gcode_path/estimates_json/error/log via the same helpers.

Job states (linear pipeline plus terminal states):
    created -> fetching -> inspecting -> awaiting_confirm -> slicing
        -> uploading -> printing -> done
    failed / cancelled are reachable from any non-terminal state.

`awaiting_confirm` is stable across restarts; `restart_recovery()` (in
workers.py) decides what happens to every other in-flight state on boot.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source_url TEXT,
    source_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    claimed_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    input_path TEXT,
    gcode_path TEXT,
    plate INTEGER NOT NULL DEFAULT 0,
    plates INTEGER NOT NULL DEFAULT 1,
    thumbnail_path TEXT,
    palette_json TEXT NOT NULL DEFAULT '[]',
    plan_json TEXT NOT NULL DEFAULT '[]',
    slot_spools_json TEXT NOT NULL DEFAULT '{}',
    slot_toolheads_json TEXT NOT NULL DEFAULT '{}',
    estimates_json TEXT,
    progress REAL NOT NULL DEFAULT 0,
    printer_state TEXT,
    error TEXT,
    error_code TEXT,
    log_json TEXT NOT NULL DEFAULT '[]',
    dimensions_json TEXT,
    scale_json TEXT NOT NULL DEFAULT '{"x": 1.0, "y": 1.0, "z": 1.0}',
    copies INTEGER NOT NULL DEFAULT 1,
    file_id TEXT,
    quality TEXT NOT NULL DEFAULT 'standard',
    timelapse INTEGER NOT NULL DEFAULT 0,
    final_snapshot_path TEXT,
    timelapse_path TEXT
);
"""

TERMINAL_STATES = {"done", "failed", "cancelled"}

# Allowed forward transitions; failed/cancelled permitted from any state.
TRANSITIONS = {
    "created": {"fetching", "inspecting", "failed", "cancelled"},
    "fetching": {"inspecting", "failed", "cancelled"},
    "inspecting": {"awaiting_confirm", "failed", "cancelled"},
    "awaiting_confirm": {"slicing", "failed", "cancelled"},
    "slicing": {"uploading", "failed", "cancelled"},
    "uploading": {"printing", "failed", "cancelled"},
    "printing": {"done", "failed", "cancelled"},
    "done": set(),
    "failed": set(),
    "cancelled": set(),
}

LOG_CAP = 300


class InvalidTransitionError(ValueError):
    """Raised when a job status change violates the state machine."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.executescript(SCHEMA)
    # Lightweight migrations for pre-existing databases (CREATE TABLE IF NOT
    # EXISTS won't add columns to an old table).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    for col, ddl in (
        ("dimensions_json", "TEXT"),
        ("scale_json", "TEXT NOT NULL DEFAULT '{\"x\": 1.0, \"y\": 1.0, \"z\": 1.0}'"),
        ("copies", "INTEGER NOT NULL DEFAULT 1"),
        ("file_id", "TEXT"),
        ("quality", "TEXT NOT NULL DEFAULT 'standard'"),
        ("timelapse", "INTEGER NOT NULL DEFAULT 0"),
        ("final_snapshot_path", "TEXT"),
        ("timelapse_path", "TEXT"),
        ("supports_mode", "TEXT NOT NULL DEFAULT 'model'"),
        ("slot_toolheads_json", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
    conn.commit()
    return conn


def create_job(
    conn: sqlite3.Connection,
    job_id: str,
    title: str,
    source_url: str | None,
    source_type: str,
    status: str = "created",
) -> None:
    now = _now()
    conn.execute(
        "INSERT INTO jobs(id, title, source_url, source_type, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (job_id, title, source_url, source_type, status, now, now),
    )
    conn.commit()


def get_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def list_jobs(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()


def update_job(conn: sqlite3.Connection, job_id: str, **fields) -> None:
    """Update arbitrary job columns (no transition validation)."""
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
    conn.commit()


def set_status(conn: sqlite3.Connection, job_id: str, new_status: str) -> sqlite3.Row:
    """Validated state transition. Raises InvalidTransitionError or KeyError."""
    row = get_job(conn, job_id)
    if row is None:
        raise KeyError(job_id)
    current = row["status"]
    if new_status != current and new_status not in TRANSITIONS.get(current, set()):
        raise InvalidTransitionError(f"cannot move job {job_id} from {current!r} to {new_status!r}")
    update_job(conn, job_id, status=new_status)
    return get_job(conn, job_id)


def append_log(conn: sqlite3.Connection, job_id: str, level: str, msg: str) -> None:
    row = get_job(conn, job_id)
    if row is None:
        raise KeyError(job_id)
    try:
        log = json.loads(row["log_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        log = []
    log.append({"ts": _now(), "level": level, "msg": msg})
    log = log[-LOG_CAP:]  # cap the log; keep the most recent entries
    update_job(conn, job_id, log_json=json.dumps(log))


def claim(conn: sqlite3.Connection, job_id: str, owner: str, expect_status: str | None = None) -> bool:
    """Atomically claim a job. True iff this caller won the claim."""
    if expect_status:
        cur = conn.execute(
            "UPDATE jobs SET claimed_by=?, updated_at=? WHERE id=? AND claimed_by IS NULL AND status=?",
            (owner, _now(), job_id, expect_status),
        )
    else:
        cur = conn.execute(
            "UPDATE jobs SET claimed_by=?, updated_at=? WHERE id=? AND claimed_by IS NULL",
            (owner, _now(), job_id),
        )
    conn.commit()
    return cur.rowcount == 1


def release_claim(conn: sqlite3.Connection, job_id: str) -> None:
    update_job(conn, job_id, claimed_by=None)


def claim_next(conn: sqlite3.Connection, status: str, owner: str) -> str | None:
    """Atomically claim the oldest unclaimed job in `status`. Returns job id or None."""
    row = conn.execute(
        "SELECT id FROM jobs WHERE status=? AND claimed_by IS NULL ORDER BY created_at LIMIT 1",
        (status,),
    ).fetchone()
    if row is None:
        return None
    if claim(conn, row["id"], owner, expect_status=status):
        return row["id"]
    return None
