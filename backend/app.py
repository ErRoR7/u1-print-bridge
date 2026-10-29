"""FastAPI backend for the U1 one-click-print bridge.

Routes (JSON unless noted):
  GET  /api/health                 service + integration probes
  GET  /api/config                 public config surface
  POST /api/jobs                   {"source_url"} -> create job from URL
  POST /api/jobs/upload            multipart file -> create job from upload
  GET  /api/jobs                    newest first
  GET  /api/jobs/{id}
  GET  /api/jobs/{id}/thumbnail    image/png
  GET  /api/jobs/{id}/color-preview  isometric render in mapped colors
  POST /api/jobs/{id}/file         multipart; re-run inspect for needs_file jobs
  POST /api/jobs/{id}/reprint      new job from the same source; full confirm flow again
  POST /api/jobs/{id}/confirm      {"slot_spools": {"0": 12}, "plate": 0}
  POST /api/jobs/{id}/pause|resume|cancel
  DELETE /api/jobs/{id}
  GET  /api/spools                 Spoolman passthrough (read-only)

The app never crashes at startup when Moonraker/Spoolman/ntfy are unreachable:
probes report them red and the API keeps serving.
"""
from __future__ import annotations

import json
import re
import logging
import os
import shutil
import threading
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import colors, db, fetchers, moonraker, ntfy, preview, spoolman, workers
from .config import config

log = logging.getLogger("u1-bridge.app")

STARTED_AT = datetime.now(timezone.utc).isoformat()

# Slicer quality presets: key -> upstream OrcaSlicer v2.4.2 U1 process profile
# name. The slicer image flattens each of these at build time.
QUALITIES = {
    "draft": "0.28 Extra Draft @Snapmaker U1 (0.4 nozzle)",
    "standard": "0.20 Standard @Snapmaker U1 (0.4 nozzle)",
    "fine": "0.12 Fine @Snapmaker U1 (0.4 nozzle)",
    "extrafine": "0.08 Extra Fine @Snapmaker U1 (0.4 nozzle)",
}
QUALITY_LABELS = {
    "draft": "Draft (0.28mm — fastest)",
    "standard": "Standard (0.20mm)",
    "fine": "Fine (0.12mm)",
    "extrafine": "Extra fine (0.08mm — slowest)",
}
# Overridable for Docker: mount the frontend at /app/frontend.
FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", Path(__file__).resolve().parent.parent / "frontend"))

# ------------------------------------------------------------------ startup

@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir = Path(config.DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(data_dir / "jobs.db")
    try:
        with conn() as c:
            summary = workers.restart_recovery(c)
        log.info("restart recovery: %s", summary)
    except Exception as exc:
        log.exception("restart recovery failed: %s", exc)

    def _reattach():
        try:
            with conn() as c:
                created = [r["id"] for r in c.execute("SELECT id FROM jobs WHERE status='created'")]
                printing = [r["id"] for r in c.execute("SELECT id FROM jobs WHERE status='printing'")]
            for jid in created:
                workers.spawn_pipeline(db_path, jid)
            for jid in printing:
                threading.Thread(target=workers.reattach_monitor, args=(db_path, jid),
                                 daemon=True, name=f"reattach-{jid[:8]}").start()
            threading.Thread(target=workers.supervise_uploads, args=(db_path, _supervisor_stop),
                             daemon=True, name="upload-supervisor").start()
        except Exception as exc:
            log.exception("worker reattach failed: %s", exc)

    def _probes():
        try:
            for name, probe in _probe_all().items():
                log.info("startup probe %s: %s", name, probe)
        except Exception as exc:
            log.exception("startup probes failed: %s", exc)

    threading.Thread(target=_reattach, daemon=True, name="reattach").start()
    threading.Thread(target=_probes, daemon=True, name="startup-probes").start()
    log.info("U1 print bridge %s started (data dir %s)", config.VERSION, data_dir)
    yield
    _supervisor_stop.set()


app = FastAPI(title="U1 Print Bridge", version=config.VERSION, lifespan=lifespan)
_supervisor_stop = threading.Event()


@contextmanager
def conn():
    c = db.open_db(str(Path(config.DATA_DIR) / "jobs.db"))
    try:
        yield c
    finally:
        c.close()


def _job_dict(row) -> dict:
    palette = json.loads(row["palette_json"] or "[]")
    plan = json.loads(row["plan_json"] or "[]")
    slot_spools = json.loads(row["slot_spools_json"] or "{}")
    estimates = _maybe_backfill_time(row)
    log_entries = json.loads(row["log_json"] or "[]")
    dimensions = json.loads(row["dimensions_json"]) if row["dimensions_json"] else None
    try:
        scale = json.loads(row["scale_json"] or '{"x": 1.0, "y": 1.0, "z": 1.0}')
    except (json.JSONDecodeError, TypeError):
        scale = {"x": 1.0, "y": 1.0, "z": 1.0}
    return {
        "id": row["id"],
        "title": row["title"],
        "source_url": row["source_url"],
        "source_type": row["source_type"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "palette": palette,
        "plan": plan,
        "slot_spools": slot_spools,
        "plates": row["plates"],
        "plate": row["plate"],
        "thumbnail": f"/api/jobs/{row['id']}/thumbnail" if row["thumbnail_path"] else None,
        "estimates": estimates,
        "progress": row["progress"],
        "printer_state": row["printer_state"],
        "error": row["error"],
        "error_code": row["error_code"],
        "log": log_entries,
        "dimensions": dimensions,
        "scale": scale,
        "copies": row["copies"] if row["copies"] else 1,
        "quality": row["quality"] or "standard",
        "timelapse": bool(row["timelapse"]),
        "supports": row["supports_mode"] or "model",
        "final_snapshot": f"/api/jobs/{row['id']}/snapshot" if row["final_snapshot_path"] else None,
        "timelapse_video": f"/api/jobs/{row['id']}/timelapse" if row["timelapse_path"] else None,
    }


def _get_job_or_404(c, job_id: str):
    row = db.get_job(c, job_id)
    if row is None:
        raise HTTPException(404, "job not found")
    return row


# ------------------------------------------------------------------ health

def _probe_all() -> dict:
    return {
        "moonraker": moonraker.health(config.PRINTER_URL),
        "spoolman": spoolman.health(config.SPOOLMAN_URL),
        "ntfy": ntfy.health(config.NTFY_SERVER, config.NTFY_TOPIC),
    }


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "version": config.VERSION,
        "started_at": STARTED_AT,
        "services": _probe_all(),
    }


@app.get("/api/config")
def get_config():
    return {
        "spoolman_url": config.SPOOLMAN_URL,
        "printer_url": config.PRINTER_URL,
        "ntfy_enabled": bool(config.NTFY_TOPIC),
        "version": config.VERSION,
        "qualities": QUALITY_LABELS,
    }


# -------------------------------------------------------------------- jobs

def _valid_url(url: str) -> bool:
    try:
        p = urlparse(url.strip())
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


@app.post("/api/jobs", status_code=201)
def create_job(body: dict):
    source_url = (body.get("source_url") or "").strip()
    if not _valid_url(source_url):
        raise HTTPException(422, "source_url must be an http(s) URL")
    source_type = fetchers.detect_source_type(source_url)
    file_id = body.get("file_id")
    if file_id is not None and not isinstance(file_id, (str, int)):
        raise HTTPException(422, "file_id must be a string")
    job_id = uuid.uuid4().hex
    with conn() as c:
        db.create_job(c, job_id, title=source_url, source_url=source_url, source_type=source_type)
        if file_id is not None:
            db.update_job(c, job_id, file_id=str(file_id))
        db.append_log(c, job_id, "info", f"Job created from {source_type} URL.")
    workers.spawn_pipeline(str(Path(config.DATA_DIR) / "jobs.db"), job_id)
    with conn() as c:
        return {"job": _job_dict(db.get_job(c, job_id))}


@app.post("/api/printables/files")
def printables_files(body: dict):
    """List the downloadable 3MF/STL files on a Printables model page.

    Used by the file picker so the user chooses which file to print when a
    page has several. Returns {title, files: [{id, name, kind}]}.
    """
    url = (body.get("url") or "").strip()
    if not _valid_url(url) or not fetchers.PRINTABLES_MODEL_RE.search(url):
        raise HTTPException(422, "url must be a Printables model URL")
    try:
        title, files = fetchers.list_printables_files(url)
    except fetchers.FetchError as exc:
        raise HTTPException(502, str(exc))
    return {"title": title, "files": files}


@app.post("/api/jobs/upload", status_code=201)
async def upload_job(file: UploadFile = File(...)):
    name = file.filename or "upload"
    ext = Path(name).suffix.lower()
    if ext not in (".3mf", ".stl", ".obj"):
        raise HTTPException(422, "only .3mf, .stl or .obj files are accepted")
    job_id = uuid.uuid4().hex
    jdir = Path(config.DATA_DIR) / "jobs" / job_id
    jdir.mkdir(parents=True, exist_ok=True)
    dest = jdir / f"upload{ext}"
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    with conn() as c:
        db.create_job(
            c, job_id, title=Path(name).stem, source_url=None, source_type="upload",
            status="inspecting",
        )
        db.update_job(c, job_id, input_path=str(dest))
        db.append_log(c, job_id, "info", f"Uploaded '{name}'; inspecting.")
    db_path = str(Path(config.DATA_DIR) / "jobs.db")
    threading.Thread(target=workers.rerun_inspect, args=(db_path, job_id, True), daemon=True).start()
    with conn() as c:
        return {"job": _job_dict(db.get_job(c, job_id))}


@app.get("/api/jobs")
def list_jobs():
    with conn() as c:
        return {"jobs": [_job_dict(r) for r in db.list_jobs(c)]}


_TIME_BACKFILL_DONE: set[str] = set()


def _maybe_backfill_time(row) -> dict | None:
    """Estimates dict for a job row, backfilling a missing time estimate.

    Jobs sliced before the footer-aware parser have time_s=null; parse the
    G-code once per process and persist it so the ETA shows everywhere.
    Returns None when there are no estimates at all.
    """
    job_id = row["id"]
    try:
        est = json.loads(row["estimates_json"]) if row["estimates_json"] else None
    except (ValueError, TypeError):
        est = None
    if est is None:
        return None
    if est.get("time_s") or job_id in _TIME_BACKFILL_DONE:
        return est
    gcode = row["gcode_path"]
    if not gcode or not Path(gcode).is_file():
        return est
    _TIME_BACKFILL_DONE.add(job_id)
    secs = preview.backfill_time_estimate(Path(gcode))
    if secs:
        est["time_s"] = secs
        with conn() as c:
            c.execute("UPDATE jobs SET estimates_json=? WHERE id=?",
                      (json.dumps(est), job_id))
            c.commit()
    return est


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with conn() as c:
        return {"job": _job_dict(_get_job_or_404(c, job_id))}


@app.get("/api/jobs/{job_id}/thumbnail")
def get_thumbnail(job_id: str):
    with conn() as c:
        row = _get_job_or_404(c, job_id)
        path = row["thumbnail_path"]
    if not path or not Path(path).is_file():
        raise HTTPException(404, "no thumbnail")
    return FileResponse(path, media_type="image/png")



def _parse_hex6(v):
    m = re.match(r"^#?([0-9a-fA-F]{6})$", (v or "").strip())
    if not m:
        return None
    h = m.group(1)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


@app.get("/api/jobs/{job_id}/color-preview")
def color_preview(job_id: str, colors: str = ""):
    """Isometric render of the sliced toolpath in the mapped colors.

    colors: comma-separated "gcode-tool:hex" pairs, e.g. "0:FF0000,1:FFFFFF".
    Generates the point data on demand for jobs sliced before it existed.
    """
    import numpy as np

    with conn() as c:
        row = _get_job_or_404(c, job_id)
        gcode = row["gcode_path"]
    job_dir = Path(config.DATA_DIR) / "jobs" / job_id
    npz_path = job_dir / "preview_points.npz"
    if gcode and Path(gcode).is_file():
        if not preview.ensure_preview_points(Path(gcode), npz_path):
            raise HTTPException(404, "no extrusion moves found in G-code")
    if not npz_path.is_file():
        raise HTTPException(404, "no color-preview data for this job")
    try:
        d = np.load(str(npz_path))
        xyz = d["xyz"]
        tools = d["tool"]
    except Exception as exc:
        raise HTTPException(500, f"could not read preview data: {exc}")
    if len(xyz) == 0:
        raise HTTPException(404, "no preview points")

    color_by_tool: dict[int, tuple[int, int, int]] = {}
    for part in colors.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        t, hx = part.split(":", 1)
        try:
            ti = int(t)
        except ValueError:
            continue
        rgb = _parse_hex6(hx)
        if rgb:
            color_by_tool[ti] = rgb
    return Response(content=preview.render_preview_png(xyz, tools, color_by_tool),
                    media_type="image/png")


@app.get("/api/jobs/{job_id}/snapshot")
def get_final_snapshot(job_id: str):
    """The camera photo taken when the print finished."""
    with conn() as c:
        row = _get_job_or_404(c, job_id)
        path = row["final_snapshot_path"]
    if not path or not Path(path).is_file():
        raise HTTPException(404, "no snapshot")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/jobs/{job_id}/timelapse")
def get_timelapse(job_id: str):
    """The stitched timelapse MP4 (when the job had timelapse enabled)."""
    with conn() as c:
        row = _get_job_or_404(c, job_id)
        path = row["timelapse_path"]
    if not path or not Path(path).is_file():
        raise HTTPException(404, "no timelapse")
    return FileResponse(path, media_type="video/mp4",
                        filename=f"timelapse-{job_id[:8]}.mp4")


@app.post("/api/jobs/{job_id}/file")
async def attach_file(job_id: str, file: UploadFile = File(...)):
    name = file.filename or "upload"
    ext = Path(name).suffix.lower()
    if ext not in (".3mf", ".stl"):
        raise HTTPException(422, "only .3mf or .stl files are accepted here")
    with conn() as c:
        row = _get_job_or_404(c, job_id)
        if not (row["status"] == "failed" and row["error_code"] == "needs_file"):
            raise HTTPException(409, "file attach is only for jobs waiting on a manual upload")
        jdir = Path(config.DATA_DIR) / "jobs" / job_id
        jdir.mkdir(parents=True, exist_ok=True)
        dest = jdir / f"manual{ext}"
        with dest.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
        db.update_job(c, job_id, input_path=str(dest), title=Path(name).stem)
    db_path = str(Path(config.DATA_DIR) / "jobs.db")
    threading.Thread(target=workers.rerun_inspect, args=(db_path, job_id), daemon=True).start()
    with conn() as c:
        return {"job": _job_dict(db.get_job(c, job_id))}



@app.post("/api/jobs/{job_id}/confirm", status_code=202)
def confirm_job(job_id: str, body: dict):
    with conn() as c:
        row = _get_job_or_404(c, job_id)
        if row["status"] != "awaiting_confirm":
            raise HTTPException(409, f"job is {row['status']}, not awaiting confirmation")

        palette = json.loads(row["palette_json"] or "[]")
        n_slots = len(palette)
        plate = body.get("plate", 0)
        if not isinstance(plate, int) or not (0 <= plate < row["plates"]):
            raise HTTPException(422, f"plate must be an int in 0..{row['plates'] - 1}")
        slot_spools = body.get("slot_spools") or {}
        if not isinstance(slot_spools, dict):
            raise HTTPException(422, "slot_spools must be an object")
        slot_toolheads = body.get("slot_toolheads") or {}
        if not isinstance(slot_toolheads, dict):
            raise HTTPException(422, "slot_toolheads must be an object")
        for k, v in slot_toolheads.items():
            if not (isinstance(k, str) and k.isdigit() and 0 <= int(k) < n_slots):
                raise HTTPException(422, f"unknown palette slot: {k!r}")
            if not isinstance(v, int) or not (0 <= v <= 3):
                raise HTTPException(422, f"toolhead for slot {k} must be 0..3")
        if n_slots > 1 and not slot_toolheads:
            # Stale frontend (pre-v1.0.9 JS) that doesn't send the toolhead
            # mapping: fail loudly instead of silently ignoring the picks.
            raise HTTPException(422, "toolhead mapping missing; please hard-refresh the page and try again")
        for k, v in slot_spools.items():
            if not (isinstance(k, str) and k.isdigit() and 0 <= int(k) < n_slots):
                raise HTTPException(422, f"unknown palette slot: {k!r}")
            if v is None:
                continue  # unassigned slot: allowed, means "no spool chosen"
            if not isinstance(v, int):
                raise HTTPException(422, f"spool id for slot {k} must be an int")
        # Drop unassigned (null) slots before storing.
        slot_spools = {k: v for k, v in slot_spools.items() if v is not None}

        # Optional scale factors + copy count (set in the confirm modal).
        scale = body.get("scale") or {"x": 1.0, "y": 1.0, "z": 1.0}
        if not isinstance(scale, dict):
            raise HTTPException(422, "scale must be an object like {\"x\": 1, \"y\": 1, \"z\": 1}")
        try:
            scale = {ax: float(scale.get(ax, 1.0)) for ax in ("x", "y", "z")}
        except (TypeError, ValueError):
            raise HTTPException(422, "scale axes must be numbers")
        if any(not (0.1 <= v <= 10.0) for v in scale.values()):
            raise HTTPException(422, "scale per axis must be between 10% and 1000%")
        copies = body.get("copies", 1)
        if isinstance(copies, bool) or not isinstance(copies, int) or not (1 <= copies <= 25):
            raise HTTPException(422, "copies must be an integer from 1 to 25")

        quality = body.get("quality", "standard")
        if quality not in QUALITIES:
            raise HTTPException(422, f"quality must be one of {sorted(QUALITIES)}")
        timelapse = body.get("timelapse", False)
        if not isinstance(timelapse, bool):
            raise HTTPException(422, "timelapse must be true or false")

        supports = body.get("supports", "model")
        if supports not in ("model", "on", "off", "organic"):
            raise HTTPException(422, "supports must be one of: model, on, off, organic")

        # Fit check against the 270mm U1 bed (skipped when dims unknown).
        dims = json.loads(row["dimensions_json"] or "null")
        if dims:
            sw, sd, sh = dims["x"] * scale["x"], dims["y"] * scale["y"], dims["z"] * scale["z"]
            if sw > 270 or sd > 270 or sh > 270:
                raise HTTPException(
                    422,
                    f"Scaled size {sw:.0f}x{sd:.0f}x{sh:.0f}mm exceeds the 270mm U1 bed. "
                    "Reduce the scale.",
                )
            gap = 8.0
            cols = max(1, int((270 + gap) // (sw + gap)))
            rows = max(1, int((270 + gap) // (sd + gap)))
            if copies > cols * rows:
                raise HTTPException(
                    422,
                    f"Only {cols * rows} copie(s) of that size fit on the bed. "
                    "Reduce copies or scale.",
                )

        # Preflight: never queue behind a running print, and fail fast when
        # the printer can't be reached.
        try:
            pstate = moonraker.preflight_state(config.PRINTER_URL)
        except moonraker.MoonrakerError:
            raise HTTPException(409, "Printer unreachable; check it and retry")
        if pstate in ("printing", "paused"):
            return JSONResponse(
                status_code=409,
                content={"detail": "Printer is busy", "printer_state": pstate},
            )

        try:
            db.set_status(c, job_id, "slicing")
        except db.InvalidTransitionError as exc:
            raise HTTPException(409, str(exc))
        # Refresh the plan's per-slot filament snapshots from the PRINTER's
        # live toolhead state (not Spoolman). The user maps palette slots to
        # physical toolheads; we read what the printer reports is actually
        # loaded there (color + material) and the slicer builds filament
        # profiles from that. Spoolman is no longer in the critical path.
        # Slots without a toolhead mapping keep spool=None -> the slicer
        # falls back to the plan's required color/material.
        try:
            printer_heads = moonraker.toolhead_filaments(config.PRINTER_URL) or []
        except Exception:
            printer_heads = []
        plan = json.loads(row["plan_json"] or "[]")
        for entry in plan:
            slot_key = str(entry.get("slot"))
            th = slot_toolheads.get(slot_key)
            spool = None
            if th is not None and 0 <= th < len(printer_heads):
                ph = printer_heads[th] or {}
                if ph.get("color_hex") or ph.get("material"):
                    spool = {"color_hex": ph.get("color_hex"),
                             "material": ph.get("material"),
                             "source": "printer"}
            entry["spool"] = spool
        db.update_job(c, job_id, slot_spools_json=json.dumps(slot_spools),
                      slot_toolheads_json=json.dumps(slot_toolheads),
                      plan_json=json.dumps(plan),
                      plate=plate, claimed_by=None,
                      scale_json=json.dumps(scale), copies=copies,
                      quality=quality, timelapse=1 if timelapse else 0,
                      supports_mode=supports)
        db.append_log(c, job_id, "info",
                      f"Confirmed: plate {plate + 1}, toolheads {slot_toolheads}. Queued for slicing.")
        return {"job": _job_dict(db.get_job(c, job_id))}


@app.post("/api/jobs/{job_id}/reprint", status_code=201)
def reprint_job(job_id: str):
    """Start a new job from an old job's source.

    Not one-click: the new job runs the full pipeline (fetch -> inspect ->
    awaiting_confirm) so the user fills out the print details again in the
    confirm modal. For uploads the original file is copied; for URLs the
    model is re-downloaded (preserving the picked Printables file_id).
    """
    with conn() as c:
        src = _get_job_or_404(c, job_id)
        new_id = uuid.uuid4().hex
        db_path = str(Path(config.DATA_DIR) / "jobs.db")
        if (src["source_type"] == "upload" and src["input_path"]
                and Path(src["input_path"]).is_file()):
            jdir = Path(config.DATA_DIR) / "jobs" / new_id
            jdir.mkdir(parents=True, exist_ok=True)
            dest = jdir / Path(src["input_path"]).name
            shutil.copy2(src["input_path"], dest)
            db.create_job(c, new_id, title=src["title"], source_url=None,
                          source_type="upload", status="inspecting")
            db.update_job(c, new_id, input_path=str(dest))
            db.append_log(c, new_id, "info",
                          f"Reprint of '{src['title']}' from the uploaded file; inspecting.")
            threading.Thread(target=workers.rerun_inspect,
                             args=(db_path, new_id, True), daemon=True).start()
        elif src["source_url"]:
            source_type = fetchers.detect_source_type(src["source_url"])
            db.create_job(c, new_id, title=src["title"] or src["source_url"],
                          source_url=src["source_url"], source_type=source_type)
            if src["file_id"]:
                db.update_job(c, new_id, file_id=src["file_id"])
            db.append_log(c, new_id, "info", f"Reprint of '{src['title']}'; downloading.")
            workers.spawn_pipeline(db_path, new_id)
        else:
            raise HTTPException(
                409, "Nothing to reprint: the original file is gone and there is no source link.")
        return {"job": _job_dict(db.get_job(c, new_id))}


def _require_active(c, job_id: str):
    row = _get_job_or_404(c, job_id)
    if row["status"] in db.TERMINAL_STATES:
        raise HTTPException(409, f"job is {row['status']}")
    return row


@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str):
    with conn() as c:
        row = _require_active(c, job_id)
        if row["status"] != "printing" or row["printer_state"] == "paused":
            raise HTTPException(409, "job is not printing")
        try:
            moonraker.pause(config.PRINTER_URL)
        except moonraker.MoonrakerError as exc:
            raise HTTPException(502, f"printer pause failed: {exc}")
        db.update_job(c, job_id, printer_state="paused")
        db.append_log(c, job_id, "info", "Paused from the bridge.")
        return {"job": _job_dict(db.get_job(c, job_id))}


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str):
    with conn() as c:
        row = _require_active(c, job_id)
        if row["status"] != "printing" or row["printer_state"] != "paused":
            raise HTTPException(409, "job is not paused")
        try:
            moonraker.resume(config.PRINTER_URL)
        except moonraker.MoonrakerError as exc:
            raise HTTPException(502, f"printer resume failed: {exc}")
        db.update_job(c, job_id, printer_state="printing")
        db.append_log(c, job_id, "info", "Resumed from the bridge.")
        return {"job": _job_dict(db.get_job(c, job_id))}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    with conn() as c:
        row = _require_active(c, job_id)
        if row["status"] == "printing":
            try:
                moonraker.cancel(config.PRINTER_URL)
            except moonraker.MoonrakerError as exc:
                log.warning("printer cancel failed for %s: %s", job_id, exc)
        try:
            db.set_status(c, job_id, "cancelled")
        except db.InvalidTransitionError as exc:
            raise HTTPException(409, str(exc))
        db.update_job(c, job_id, claimed_by=None)
        db.append_log(c, job_id, "info", "Cancelled from the bridge.")
        return {"job": _job_dict(db.get_job(c, job_id))}


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str):
    with conn() as c:
        row = _get_job_or_404(c, job_id)
        if row["status"] not in db.TERMINAL_STATES:
            if row["status"] == "printing":
                try:
                    moonraker.cancel(config.PRINTER_URL)
                except moonraker.MoonrakerError as exc:
                    log.warning("printer cancel failed for %s: %s", job_id, exc)
            try:
                db.set_status(c, job_id, "cancelled")
            except db.InvalidTransitionError:
                pass
        c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        c.commit()
    shutil.rmtree(Path(config.DATA_DIR) / "jobs" / job_id, ignore_errors=True)
    return None


# ------------------------------------------------------------------ spools

@app.get("/api/spools")
def get_spools():
    try:
        return {"spools": spoolman.get_spools(config.SPOOLMAN_URL)}
    except spoolman.SpoolmanError as exc:
        raise HTTPException(502, f"Spoolman unavailable: {exc}")


@app.get("/api/toolheads")
def get_toolheads():
    """What's loaded on each toolhead T0..T3.

    Source priority (first hit wins per toolhead):
    1. Spoolman location naming (T0..T3) -- the user's own inventory record,
       authoritative for which spool is where.
    2. The printer itself: PAXX's print_task_config object reports the
       per-extruder filament color (same data Fluidd's AFC panel shows).
       Used when Spoolman has no located spool; matched by nearest color
       (dE < 35) against spools *without* a toolhead location.
    3. None -- nothing detected at that toolhead.

    When both sources exist but disagree significantly, `mismatch` is set so
    the UI can warn (stale Spoolman location or misreported printer color).

    Each entry: {toolhead, color_hex, material, spool|null, source,
                 printer_color_hex|null, mismatch: bool}.
    """
    try:
        printer = moonraker.toolhead_filaments(config.PRINTER_URL)
    except Exception:  # never break the endpoint on a printer hiccup
        printer = None
    try:
        spools = spoolman.get_spools(config.SPOOLMAN_URL)
    except spoolman.SpoolmanError as exc:
        if printer is None:
            raise HTTPException(502, f"Spoolman unavailable: {exc}")
        spools = []

    located_ids = set()
    for sp in spools:
        loc = (sp.get("location") or "").upper().replace(" ", "")
        if any(f"T{n}" in loc for n in range(4)):
            located_ids.add(sp.get("id"))
    unlocated = [sp for sp in spools if sp.get("id") not in located_ids]

    out = []
    for n in range(4):
        entry = {"toolhead": f"T{n}", "color_hex": None, "material": None,
                 "spool": None, "source": "none", "printer_color_hex": None,
                 "mismatch": False}
        pc = printer[n].get("color_hex") if printer is not None else None
        entry["printer_color_hex"] = pc
        found = spoolman.spools_at_toolhead(spools, n)
        if found:
            s = found[0]
            entry.update({"color_hex": colors.normalize_hex(s.get("color_hex")),
                          "material": s.get("material"), "spool": s,
                          "source": "spoolman"})
            if pc and entry["color_hex"]:
                try:
                    if colors.delta_e(pc, entry["color_hex"]) >= 35:
                        entry["mismatch"] = True
                except Exception:
                    pass
        elif pc:
            entry["color_hex"] = pc
            entry["material"] = printer[n].get("material")
            if unlocated:
                best, de = colors.nearest_spool(pc, unlocated)
                if best is not None and de is not None and de < 35:
                    entry["spool"] = best
                    entry["material"] = entry["material"] or best.get("material")
            entry["source"] = "printer"
        out.append(entry)
    return {"toolheads": out}


@app.get("/api/camera")
def get_camera():
    """Live camera URLs for the U1, discovered via Moonraker.

    Never raises: reports {"available": False, ...} instead.
    """
    try:
        cam = moonraker.camera_urls(config.PRINTER_URL)
    except Exception:
        cam = None
    if cam is None:
        return {"available": False, "detail": "no enabled webcam reported by Moonraker"}
    return {"available": True, **cam}





if FRONTEND_DIR.is_dir():
    @app.middleware("http")
    async def _no_cache_html(request, call_next):
        resp = await call_next(request)
        # The ?v= cache-bust only works if index.html itself is fresh.
        # Never cache HTML; versioned assets (app.js?v=, styles.css?v=) are
        # still cached by the browser per their query strings.
        if request.url.path.endswith((".html", "/")):
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
        return resp

    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
