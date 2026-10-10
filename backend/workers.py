"""Background workers for the U1 print bridge.

Pipeline thread:   created -> fetching -> inspecting -> awaiting_confirm
Upload+monitor:    uploading -> printing -> done/failed (claimed via claimed_by)
Slicer:            separate container/service (slicer/worker.py) owns `slicing`.

SPOOLMAN IS READ-ONLY HERE. The bridge reads spool color/material/remaining
grams to build the color plan and to sanity-check filament sufficiency, but it
never writes: the printer itself deducts filament usage from Spoolman via its
own tracking. There is intentionally no deduction step on job completion.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx

from . import colors, db, fetchers, moonraker, ntfy, spoolman, threedmf
from .config import config

log = logging.getLogger("u1-bridge.workers")

POLL_S = 5.0
LOW_SPOOL_WARN_G = 30.0

# Timelapse: one camera frame every N seconds while printing.
TIMELAPSE_INTERVAL_S = 30.0
_timelapse_last: dict[str, float] = {}

# ntfy event per pipeline stage on failure
_FAIL_EVENTS = {
    "fetching": "fetch_failed",
    "inspecting": "inspect_failed",
    "slicing": "slice_failed",
    "uploading": "upload_failed",
    "printing": "print_failed",
}


def job_dir(db_path: str, job_id: str) -> Path:
    return Path(config.DATA_DIR) / "jobs" / job_id


def fail_job(conn, job_id: str, error: str, error_code: str, stage: str | None = None) -> None:
    """Move a job to failed, log it, and notify via ntfy."""
    row = db.get_job(conn, job_id)
    if row is None:
        return
    try:
        db.set_status(conn, job_id, "failed")
    except db.InvalidTransitionError:
        return  # already terminal
    db.update_job(conn, job_id, error=error, error_code=error_code)
    db.append_log(conn, job_id, "error", error)
    log.warning("job %s failed [%s]: %s", job_id, error_code, error)
    if stage:
        ntfy.notify(
            config.NTFY_SERVER, config.NTFY_TOPIC,
            _FAIL_EVENTS.get(stage, "failed"),
            {"id": job_id, "title": row["title"], "error": error},
            bridge_url=config.BRIDGE_URL,
        )


# ---------------------------------------------------------------- pipeline

# Filament densities (g/cm^3) for pre-slice usage estimates.
_DENSITY = {"PLA": 1.24, "PETG": 1.27, "PET": 1.27, "ABS": 1.04, "ASA": 1.07,
            "TPU": 1.21, "PA": 1.14, "PC": 1.20}
_DEFAULT_DENSITY = 1.24


def _density_for(material: str | None) -> float:
    if material:
        m = material.upper()
        for key, d in _DENSITY.items():
            if key in m:
                return d
    return _DEFAULT_DENSITY


def _build_plan(conn, job_id: str, inspection: threedmf.Inspection) -> tuple[list[dict], dict]:
    """Build the color plan against live Spoolman data (read-only).

    Returns (plan, proposed_slot_spools). Match grades:
      ok      dE < 12
      close   dE < 35
      missing no spool at T{i}, or dE >= 35
      unknown Spoolman unreachable
    """
    try:
        spools = spoolman.get_spools(config.SPOOLMAN_URL)
        spoolman_ok = True
    except spoolman.SpoolmanError as exc:
        log.warning("Spoolman unavailable during plan build: %s", exc)
        spools = []
        spoolman_ok = False

    plan: list[dict] = []
    proposed: dict[str, int] = {}
    single_slot = len(inspection.palette) == 1 and inspection.palette[0]["hex"] is None

    for entry in inspection.palette:
        slot = entry["slot"]
        toolhead = f"T{slot}"
        required_hex = entry["hex"]
        required_material = entry["material"]
        candidates = spoolman.spools_at_toolhead(spools, slot) if spoolman_ok else []

        spool = None
        match = "unknown"
        if not spoolman_ok:
            match = "unknown"
        elif single_slot:
            # "As modeled": any spool loaded at T0 works.
            if candidates:
                spool, match = candidates[0], "ok"
            else:
                match = "missing"
        elif required_hex and candidates:
            best, de = colors.nearest_spool(required_hex, candidates)
            spool = best
            if de is None:
                match = "missing"
            elif de < 12:
                match = "ok"
            elif de < 35:
                match = "close"
            else:
                match, spool = "missing", None
        else:
            match = "missing"

        if spool is not None and match in ("ok", "close") and spool.get("id") is not None:
            proposed[str(slot)] = spool["id"]

        remaining = spool.get("remaining_g") if spool else None
        if isinstance(remaining, (int, float)) and remaining < LOW_SPOOL_WARN_G:
            db.append_log(
                conn, job_id, "warning",
                f"Spool '{spool.get('name')}' at {toolhead} is low ({remaining:.0f} g remaining).",
            )

        # Pre-slice filament estimate: per-extruder mesh volume (cm^3) from
        # the 3MF times material density. Slot N <-> 1-based extruder N+1.
        # Solid-model volume overstates real usage (infill isn't solid), so
        # this is an upper bound for picking a spool with enough left.
        density = _density_for((spool or {}).get("material") or required_material)
        grams = round(inspection.volumes.get(slot + 1, 0.0) * density, 1)
        plan.append({
            "slot": slot,
            "required_hex": required_hex,
            "required_material": required_material,
            "toolhead": toolhead,
            "spool": spool,
            # Spoolman-location toolhead for the chosen spool; the confirm UI
            # defaults the picker here (not via printer color matching).
            "spool_toolhead": slot if spool is not None else None,
            "grams": grams,
            "match": match,
        })
    return plan, proposed


def _inspect_and_plan(db_path: str, job_id: str, conn) -> None:
    row = db.get_job(conn, job_id)
    jdir = job_dir(db_path, job_id)
    db.set_status(conn, job_id, "inspecting")
    db.append_log(conn, job_id, "info", f"Inspecting {Path(row['input_path']).name} ...")
    try:
        inspection = threedmf.inspect(row["input_path"])
    except threedmf.InspectorError as exc:
        fail_job(conn, job_id, str(exc), exc.code, stage="inspecting")
        return

    if inspection.thumbnail_bytes:
        thumb = jdir / "thumbnail.png"
        thumb.write_bytes(inspection.thumbnail_bytes)
        db.update_job(conn, job_id, thumbnail_path=str(thumb))

    plan, proposed = _build_plan(conn, job_id, inspection)
    db.update_job(
        conn,
        job_id,
        palette_json=json.dumps(inspection.palette),
        plan_json=json.dumps(plan),
        slot_spools_json=json.dumps(proposed),
        plates=inspection.plates,
        plate=0,
        dimensions_json=json.dumps(inspection.dimensions) if inspection.dimensions else None,
    )
    db.append_log(
        conn, job_id, "info",
        f"Found {len(inspection.palette)} color slot(s), {inspection.plates} plate(s). "
        "Confirm spool placement to continue.",
    )
    db.set_status(conn, job_id, "awaiting_confirm")


def run_pipeline(db_path: str, job_id: str) -> None:
    """Thread body: created -> fetching -> inspecting -> awaiting_confirm."""
    conn = db.open_db(db_path)
    try:
        row = db.get_job(conn, job_id)
        if row is None or row["status"] != "created":
            return
        jdir = job_dir(db_path, job_id)
        jdir.mkdir(parents=True, exist_ok=True)

        if row["source_type"] == "upload" and row["input_path"]:
            db.append_log(conn, job_id, "info", "Using uploaded file.")
            _inspect_and_plan(db_path, job_id, conn)
            return

        db.set_status(conn, job_id, "fetching")
        db.append_log(conn, job_id, "info", f"Downloading {row['source_url']} ...")
        try:
            input_path, title, source_type = fetchers.download_model(
                row["source_url"], jdir, session_cookie=config.MAKERWORLD_SESSION_COOKIE,
                file_id=row["file_id"],
            )
        except fetchers.NeedsManualUpload as exc:
            fail_job(conn, job_id, str(exc), "needs_file", stage="fetching")
            return
        except fetchers.FetchError as exc:
            fail_job(conn, job_id, str(exc), "fetch_failed", stage="fetching")
            return

        db.update_job(conn, job_id, input_path=str(input_path), title=title, source_type=source_type)
        db.append_log(conn, job_id, "info", f"Downloaded '{title}'.")
        _inspect_and_plan(db_path, job_id, conn)
    except Exception as exc:  # never leave a job stuck mid-pipeline
        log.exception("pipeline crashed for job %s", job_id)
        try:
            fail_job(conn, job_id, f"Internal error: {exc}", "internal", stage="fetching")
        except Exception:
            pass
    finally:
        conn.close()


def spawn_pipeline(db_path: str, job_id: str) -> threading.Thread:
    t = threading.Thread(target=run_pipeline, args=(db_path, job_id), daemon=True,
                         name=f"pipeline-{job_id[:8]}")
    t.start()
    return t


# ------------------------------------------------------- upload + monitoring

def _format_duration(seconds: float | None) -> str:
    if not seconds:
        return "unknown time"
    m = int(seconds // 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def _filament_check(conn, job_id: str, row) -> str | None:
    """Read-only filament sufficiency check vs slice estimates.

    Returns an error string when a planned spool cannot cover the estimate,
    else None. Spoolman is only READ here.
    """
    estimates = json.loads(row["estimates_json"] or "null") if row["estimates_json"] else None
    if not estimates:
        db.append_log(conn, job_id, "warning", "No slice estimates; skipping filament check.")
        return None
    by_tool = estimates.get("filament_g_by_tool") or {}
    total_g = estimates.get("filament_g")
    plan = json.loads(row["plan_json"] or "[]")
    slot_spools = json.loads(row["slot_spools_json"] or "{}")
    try:
        live = {s["id"]: s for s in spoolman.get_spools(config.SPOOLMAN_URL)}
    except spoolman.SpoolmanError as exc:
        db.append_log(conn, job_id, "warning", f"Spoolman unreachable for filament check: {exc}")
        return None

    for entry in plan:
        slot = str(entry["slot"])
        spool_id = slot_spools.get(slot)
        if spool_id is None:
            continue
        need = by_tool.get(f"T{entry['slot']}")
        if need is None and len(plan) == 1 and total_g:
            need = total_g
        if need is None:
            continue
        spool = live.get(spool_id) or entry.get("spool") or {}
        remaining = spool.get("remaining_g")
        if isinstance(remaining, (int, float)) and remaining < need:
            return (
                f"not enough filament: '{spool.get('name')}' at {entry['toolhead']} "
                f"has {remaining:.0f} g but the slice needs ~{need:.0f} g"
            )
    return None


def _materials_compatible(required: str | None, actual: str | None) -> bool:
    """Lenient material-family match (PLA vs PLA+/Silk PLA, PETG vs PETG-CF).

    Unknown on either side is not a mismatch -- it just can't be checked.
    """
    if not required or not actual:
        return True
    req = re.sub(r"[^A-Z0-9]", "", required.upper())
    act = re.sub(r"[^A-Z0-9]", "", actual.upper())
    if not req or not act:
        return True
    return req == act or req in act or act in req


def _material_check(conn, job_id: str, row) -> str | None:
    """Read-only material match check: spool material vs what the model wants.

    Returns an error string when a confirmed spool's material clearly differs
    from the model's filament type, else None. Warns (does not block) when
    Spoolman is unreachable or the model declares no material.
    """
    plan = json.loads(row["plan_json"] or "[]")
    slot_spools = json.loads(row["slot_spools_json"] or "{}")
    try:
        live = {s["id"]: s for s in spoolman.get_spools(config.SPOOLMAN_URL)}
    except spoolman.SpoolmanError as exc:
        db.append_log(conn, job_id, "warning", f"Spoolman unreachable for material check: {exc}")
        return None

    for entry in plan:
        slot = str(entry["slot"])
        spool_id = slot_spools.get(slot)
        if spool_id is None:
            continue
        required = entry.get("required_material")
        if not required:
            continue  # model declares no material (e.g. plain STL) -- nothing to check
        spool = live.get(spool_id) or entry.get("spool") or {}
        actual = spool.get("material")
        if not _materials_compatible(required, actual):
            return (
                f"material mismatch: '{spool.get('name')}' at {entry['toolhead']} "
                f"is {actual or 'unknown material'} but the model calls for {required}. "
                "Load the right filament or fix the spool's material in Spoolman."
            )
    return None


def upload_monitor(db_path: str, job_id: str) -> None:
    """Thread body: uploading -> printing -> done/failed. Never deducts from
    Spoolman (read-only); the printer handles usage accounting itself."""
    conn = db.open_db(db_path)
    try:
        row = db.get_job(conn, job_id)
        if row is None:
            return

        # Preflight: refuse to stomp a running print.
        try:
            state = moonraker.preflight_state(config.PRINTER_URL)
        except moonraker.MoonrakerError as exc:
            fail_job(conn, job_id, f"Printer unreachable at upload time: {exc}",
                     "printer_unreachable", stage="uploading")
            return
        if state in ("printing", "paused"):
            fail_job(conn, job_id, "Printer became busy before upload -- confirm again once it's free.",
                     "printer_busy", stage="uploading")
            return

        # Filament sufficiency (read-only check).
        problem = _filament_check(conn, job_id, row)
        if problem:
            db.append_log(conn, job_id, "error", problem)
            fail_job(conn, job_id, problem[0].upper() + problem[1:], "filament_low", stage="uploading")
            ntfy.notify(
                config.NTFY_SERVER, config.NTFY_TOPIC, "filament_low",
                {"id": job_id, "title": row["title"], "error": problem},
                bridge_url=config.BRIDGE_URL,
            )
            return

        # Material match (read-only check).
        problem = _material_check(conn, job_id, row)
        if problem:
            db.append_log(conn, job_id, "error", problem)
            fail_job(conn, job_id, problem[0].upper() + problem[1:], "material_mismatch", stage="uploading")
            ntfy.notify(
                config.NTFY_SERVER, config.NTFY_TOPIC, "material_mismatch",
                {"id": job_id, "title": row["title"], "error": problem},
                bridge_url=config.BRIDGE_URL,
            )
            return

        dest = f"u1bridge_{job_id[:8]}.gcode"
        db.append_log(conn, job_id, "info", f"Uploading {dest} to printer (auto-start)...")
        try:
            moonraker.upload(config.PRINTER_URL, row["gcode_path"], dest, print=True)
        except moonraker.MoonrakerError as exc:
            fail_job(conn, job_id, f"Upload failed: {exc}", "upload_failed", stage="uploading")
            return

        db.set_status(conn, job_id, "printing")
        db.update_job(conn, job_id, printer_state="printing", progress=0.0)
        db.append_log(conn, job_id, "info", "Print started on the U1.")
        # No ntfy on print start -- OctoEverywhere already covers that.
        _poll_print(conn, db_path, job_id)
    except Exception as exc:
        log.exception("upload monitor crashed for job %s", job_id)
        try:
            fail_job(conn, job_id, f"Internal error: {exc}", "internal", stage="uploading")
        except Exception:
            pass
    finally:
        try:
            db.release_claim(conn, job_id)
        except Exception:
            pass
        conn.close()


def _camera_snapshot(timeout: float = 10.0) -> bytes | None:
    """Grab one JPEG frame from the printer camera. None on any failure."""
    try:
        cam = moonraker.camera_urls(config.PRINTER_URL, timeout=timeout)
    except Exception:
        return None
    if not cam:
        return None
    try:
        resp = httpx.get(cam["snapshot_url"], timeout=timeout)
        resp.raise_for_status()
        data = resp.content
        return data if data[:2] == b"\xff\xd8" else None  # must be a JPEG
    except Exception:
        return None


def _timelapse_tick(conn, db_path: str, job_id: str) -> None:
    """Save one timelapse frame if the capture interval has elapsed."""
    now = time.monotonic()
    if now - _timelapse_last.get(job_id, 0) < TIMELAPSE_INTERVAL_S:
        return
    _timelapse_last[job_id] = now
    img = _camera_snapshot()
    if img is None:
        db.append_log(conn, job_id, "warning", "Timelapse frame skipped: camera unreachable.")
        return
    frames = job_dir(db_path, job_id) / "timelapse"
    frames.mkdir(exist_ok=True)
    n = len(list(frames.glob("frame_*.jpg")))
    (frames / f"frame_{n:05d}.jpg").write_bytes(img)


def _stitch_timelapse(conn, db_path: str, job_id: str) -> str | None:
    """Stitch captured frames into an MP4. Returns the path, or None."""
    frames_dir = job_dir(db_path, job_id) / "timelapse"
    frames = sorted(frames_dir.glob("frame_*.jpg"))
    _timelapse_last.pop(job_id, None)
    if len(frames) < 2:
        db.append_log(conn, job_id, "warning", "Timelapse skipped: fewer than 2 frames captured.")
        return None
    if shutil.which("ffmpeg") is None:
        db.append_log(conn, job_id, "warning",
                      "Timelapse skipped: ffmpeg is not installed in the backend image.")
        return None
    out = job_dir(db_path, job_id) / "timelapse.mp4"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-framerate", "30", "-pattern_type", "glob",
             "-i", str(frames_dir / "frame_*.jpg"),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
            capture_output=True, text=True, timeout=900)
    except Exception as exc:
        db.append_log(conn, job_id, "warning", f"Timelapse stitch failed: {exc}")
        return None
    if proc.returncode != 0 or not out.is_file():
        db.append_log(conn, job_id, "warning",
                      f"Timelapse stitch failed (ffmpeg exit {proc.returncode}).")
        return None
    db.append_log(conn, job_id, "info", f"Timelapse saved ({len(frames)} frames).")
    return str(out)


def _poll_print(conn, db_path: str, job_id: str) -> None:
    """Poll Moonraker until the print completes/fails or the job is cancelled."""
    failures = 0
    while True:
        time.sleep(POLL_S)
        row = db.get_job(conn, job_id)
        if row is None or row["status"] != "printing":
            return  # cancelled externally
        try:
            status = moonraker.query(config.PRINTER_URL)
            failures = 0
        except moonraker.MoonrakerError as exc:
            failures += 1
            db.append_log(conn, job_id, "warning", f"Printer poll failed ({failures}): {exc}")
            if failures >= 12:
                _timelapse_last.pop(job_id, None)
                fail_job(conn, job_id, "Lost contact with the printer during the print.",
                         "printer_unreachable", stage="printing")
                return
            continue
        ps = status.get("print_stats", {})
        state = ps.get("state", "unknown")
        progress = status.get("display_status", {}).get("progress")
        if progress is None:
            progress = status.get("virtual_sdcard", {}).get("progress", 0.0)
        db.update_job(conn, job_id, printer_state=state,
                          progress=round(float(progress or 0.0) * 100, 1))

        if row["timelapse"]:
            _timelapse_tick(conn, db_path, job_id)

        # Done snapshot: capture it while the last layer is finishing
        # (progress ~99%, still "printing", bed at print height) -- NOT on
        # "complete", by which point the end G-code has already dropped the bed.
        if (not row["final_snapshot_path"] and state == "printing"
                and (progress or 0) >= 0.99):
            early = _camera_snapshot()
            if early:
                early_path = str(job_dir(db_path, job_id) / "final.jpg")
                Path(early_path).write_bytes(early)
                db.update_job(conn, job_id, final_snapshot_path=early_path)
                db.append_log(conn, job_id, "info",
                              "Captured the finished-print photo (bed still up).")

        if state == "complete":
            duration = _format_duration(ps.get("print_duration"))
            # Prefer the near-last-layer shot; fall back to a late one.
            snap_path = row["final_snapshot_path"]
            if not snap_path:
                snap = _camera_snapshot()
                if snap:
                    snap_path = str(job_dir(db_path, job_id) / "final.jpg")
                    Path(snap_path).write_bytes(snap)
            tl_path = _stitch_timelapse(conn, db_path, job_id) if row["timelapse"] else None
            db.set_status(conn, job_id, "done")
            db.update_job(conn, job_id, progress=100.0,
                          final_snapshot_path=snap_path, timelapse_path=tl_path)
            db.append_log(conn, job_id, "info", f"Print finished in {duration}.")
            # NOTE: no Spoolman write here -- the printer deducts filament
            # usage from Spoolman itself (printer-side tracking).
            ntfy.notify(
                config.NTFY_SERVER, config.NTFY_TOPIC, "done",
                {"id": job_id, "title": row["title"]},
                bridge_url=config.BRIDGE_URL, duration=duration,
                attach_path=snap_path,
            )
            return
        if state in ("error", "cancelled"):
            _timelapse_last.pop(job_id, None)
            fail_job(conn, job_id, f"Printer reported state '{state}'.",
                     "print_failed", stage="printing")
            return


def reattach_monitor(db_path: str, job_id: str) -> None:
    """Thread body for jobs that were `printing` across a bridge restart."""
    conn = db.open_db(db_path)
    try:
        if not db.claim(conn, job_id, "backend"):
            return
        row = db.get_job(conn, job_id)
        if row is None or row["status"] != "printing":
            db.release_claim(conn, job_id)
            return
        db.append_log(conn, job_id, "info", "Re-attached print monitor after restart.")
        _poll_print(conn, db_path, job_id)
    except Exception:
        log.exception("reattach monitor crashed for job %s", job_id)
    finally:
        try:
            db.release_claim(conn, job_id)
        except Exception:
            pass
        conn.close()


def rerun_inspect(db_path: str, job_id: str, first_run: bool = False) -> None:
    """Thread body for POST /api/jobs/{id}/file (and initial uploads):
    re-run inspect on the job's input file."""
    conn = db.open_db(db_path)
    try:
        db.update_job(conn, job_id, status="inspecting", error=None, error_code=None)
        if not first_run:
            db.append_log(conn, job_id, "info", "New file attached; re-running inspection.")
        _inspect_and_plan(db_path, job_id, conn)
    except Exception as exc:
        log.exception("re-inspect crashed for job %s", job_id)
        try:
            fail_job(conn, job_id, f"Internal error: {exc}", "internal", stage="inspecting")
        except Exception:
            pass
    finally:
        conn.close()


def supervise_uploads(db_path: str, stop: threading.Event) -> None:
    """Background loop: claim jobs in `uploading` and hand them to a monitor."""
    while not stop.wait(POLL_S):
        conn = db.open_db(db_path)
        try:
            job_id = db.claim_next(conn, "uploading", "backend")
            if job_id:
                t = threading.Thread(target=upload_monitor, args=(db_path, job_id),
                                     daemon=True, name=f"monitor-{job_id[:8]}")
                t.start()
        finally:
            conn.close()


# ------------------------------------------------------------- recovery

def restart_recovery(conn) -> dict:
    """Reconcile in-flight jobs after a bridge restart.

    - fetching/inspecting/slicing/uploading -> failed (safe to retry)
    - printing -> kept; caller re-attaches a monitor thread
    - created -> kept; caller re-queues the pipeline
    - awaiting_confirm -> untouched (stable across restarts)
    """
    summary = {"failed": [], "kept_printing": [], "requeued": []}
    rows = conn.execute("SELECT id, status FROM jobs WHERE status NOT IN ('done','failed','cancelled')").fetchall()
    for row in rows:
        job_id, status = row["id"], row["status"]
        if status in ("fetching", "inspecting", "slicing", "uploading"):
            try:
                db.set_status(conn, job_id, "failed")
            except db.InvalidTransitionError:
                pass
            db.update_job(
                conn, job_id,
                error=f"Bridge restarted during '{status}' \u2014 safe to retry.",
                error_code="restarted",
                claimed_by=None,
            )
            db.append_log(conn, job_id, "warning", f"Marked failed after restart (was {status}).")
            summary["failed"].append(job_id)
        elif status == "printing":
            db.update_job(conn, job_id, claimed_by=None)
            db.append_log(conn, job_id, "info", "Bridge restarted; re-attaching print monitor.")
            summary["kept_printing"].append(job_id)
        elif status == "created":
            db.append_log(conn, job_id, "info", "Re-queued after bridge restart.")
            summary["requeued"].append(job_id)
    return summary
