#!/usr/bin/env python3
"""Slicer worker for the U1 print bridge.

Standalone: stdlib + sqlite3 only. Shares the jobs DB with the backend via the
DATA_DIR volume. Every 3s it atomically claims one job in `slicing` status and:

1. Builds 4 filament JSONs from the stock U1 filament profile template,
   overriding filament_colour from the job plan's spool colors (material
   matched to a stock template when one exists, else PLA).
2. Runs the Snapmaker OrcaSlicer fork CLI headless under xvfb:
     xvfb-run -a /opt/orca/squashfs-root/AppRun --slice <plate+1>
       --load-settings "<machine>;<process>"
       --load-filaments "<f0>;<f1>;<f2>;<f3>"
       --outputdir <jobdir> <input.3mf>
3. Parses time/filament estimates from CLI stdout + gcode comments
   (tolerant regexes; null when absent).
4. On success -> status='uploading' (+gcode_path, estimates_json).
   On CLI crash (SIGSEGV / nonzero exit with extruder/config errors) ->
   rebuild a minimal 3MF and retry ONCE -> still failing -> status='failed'
   with the captured log. The claim is always released.

The input 3MF is otherwise passed to the slicer UNMODIFIED: palette slot N
maps positionally to toolhead TN (see backend/threedmf.py), so no paint data
is ever rewritten.

Env:
  DATA_DIR=/data  ORCA_BIN=/opt/orca/squashfs-root/AppRun
  ORCA_MACHINE_PROFILE=/opt/orca/profiles/machine.json
  ORCA_PROCESS_PROFILE=/opt/orca/profiles/process.json
  ORCA_FILAMENT_DIR=/opt/orca/profiles   (filament_<MATERIAL>.json templates)
"""
from __future__ import annotations

import base64
import glob
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("u1-slicer")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "jobs.db"
ORCA_BIN = os.environ.get("ORCA_BIN", "/opt/orca/squashfs-root/AppRun")
MACHINE_PROFILE = os.environ.get("ORCA_MACHINE_PROFILE", "/opt/orca/profiles/machine.json")
PROCESS_PROFILE = os.environ.get("ORCA_PROCESS_PROFILE", "/opt/orca/profiles/process.json")
FILAMENT_DIR = Path(os.environ.get("ORCA_FILAMENT_DIR", "/opt/orca/profiles"))

# Quality presets, flattened at Docker build time (see slicer/Dockerfile).
PROCESS_PROFILES = {
    "draft": str(FILAMENT_DIR / "process_draft.json"),
    "standard": str(FILAMENT_DIR / "process_standard.json"),
    "fine": str(FILAMENT_DIR / "process_fine.json"),
    "extrafine": str(FILAMENT_DIR / "process_extrafine.json"),
}


def process_profile_for(quality: str | None) -> str:
    """Process profile path for a quality key; falls back to the default."""
    p = PROCESS_PROFILES.get((quality or "standard").lower())
    if p and Path(p).is_file():
        return p
    return PROCESS_PROFILE


SUPPORTS_MODES = ("model", "on", "off", "organic")


def process_profile_with_supports(job_dir: Path, quality: str | None,
                                  supports_mode: str | None) -> str:
    """Return the process profile path, applying a supports override.

    mode "model" (default) leaves the profile untouched. "on"/"off"/"organic"
    copies the profile into the job dir with enable_support forced (and
    support_type=tree(auto) for "organic"), so the shared profile on disk is
    never mutated.
    """
    base = process_profile_for(quality)
    mode = (supports_mode or "model").lower()
    if mode not in SUPPORTS_MODES or mode == "model":
        return base
    try:
        data = json.loads(Path(base).read_text())
    except Exception as exc:
        raise RuntimeError(f"could not read process profile {base}: {exc}") from exc
    if mode == "organic":
        data["enable_support"] = "1"
        data["support_type"] = "tree(auto)"
    else:
        data["enable_support"] = "1" if mode == "on" else "0"
    out = job_dir / f"process_supports_{mode}.json"
    out.write_text(json.dumps(data, indent=2))
    return str(out)
OWNER = f"slicer-{os.getpid()}"

LOG_CAP = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _append_log(conn: sqlite3.Connection, job_id: str, level: str, msg: str) -> None:
    row = conn.execute("SELECT log_json FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return
    try:
        entries = json.loads(row["log_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        entries = []
    entries.append({"ts": _now(), "level": level, "msg": msg})
    entries = entries[-LOG_CAP:]
    conn.execute(
        "UPDATE jobs SET log_json=?, updated_at=? WHERE id=?",
        (json.dumps(entries), _now(), job_id),
    )
    conn.commit()


def _set(conn: sqlite3.Connection, job_id: str, **fields) -> None:
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
    conn.commit()


def _claim_next(conn: sqlite3.Connection) -> str | None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT id FROM jobs WHERE status='slicing' AND claimed_by IS NULL"
            " ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        cur = conn.execute(
            "UPDATE jobs SET claimed_by=?, updated_at=? WHERE id=? AND claimed_by IS NULL",
            (OWNER, _now(), row["id"]),
        )
        conn.execute("COMMIT")
        return row["id"] if cur.rowcount == 1 else None
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ------------------------------------------------------- filament profiles

def _template_for(material: str | None) -> dict:
    """Stock filament template, matched by material, else PLA."""
    mat = (material or "PLA").upper().replace(" ", "")
    for name in (f"filament_{mat}.json", "filament_PLA.json"):
        p = FILAMENT_DIR / name
        if p.is_file():
            return json.loads(p.read_text())
    raise FileNotFoundError(f"no filament template in {FILAMENT_DIR} (tried {mat}, PLA)")


def _normalize_hex6(value: str | None) -> str:
    v = (value or "").strip().lstrip("#").upper()
    if len(v) == 8:
        v = v[:6]
    return v if re.fullmatch(r"[0-9A-F]{6}", v) else "808080"


def build_filament_profiles(job_dir: Path, plan: list[dict], slot_spools: dict) -> list[str]:
    """Write 4 filament JSONs (one per toolhead T0..T3). Returns paths."""
    paths = []
    for slot in range(4):
        entry = next((e for e in plan if e.get("slot") == slot), None)
        spool = None
        if entry and str(slot) in slot_spools:
            spool = entry.get("spool") or None
        material = (spool or {}).get("material") or (entry or {}).get("required_material")
        color = (spool or {}).get("color_hex") or (entry or {}).get("required_hex")
        tpl = _template_for(material)
        tpl = dict(tpl)
        tpl["filament_colour"] = ["#" + _normalize_hex6(color)]
        base_id = tpl.get("filament_settings_id") or tpl.get("name") or "filament"
        tpl["filament_settings_id"] = f"{base_id}_T{slot}"
        tpl["name"] = f"U1 Bridge T{slot} {(material or 'PLA')}"
        out = job_dir / f"filament_T{slot}.json"
        out.write_text(json.dumps(tpl, indent=2))
        paths.append(str(out))
    return paths


# ------------------------------------------------------------- estimates

_TIME_RE = re.compile(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+(?:\.\d+)?)\s*s)?", re.I)


def _parse_hms(text: str) -> float | None:
    m = _TIME_RE.search(text)
    if not m or not m.group(0).strip():
        return None
    h, mi, s = m.group(1), m.group(2), m.group(3)
    if not (h or mi or s):
        return None
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)


def parse_estimates(stdout: str, gcode_path: Path | None) -> dict:
    """Tolerant time/filament parsing. Fields are None when not found."""
    time_s: float | None = None
    filament_g: float | None = None
    by_tool: dict | None = None

    text = stdout or ""
    comments: list[str] = []
    if gcode_path and gcode_path.is_file():
        # OrcaSlicer puts summary comments (notably the time estimate) in the
        # footer, so scan the head AND the tail of the file.
        from collections import deque
        with gcode_path.open("r", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= 4000:
                    break
                if line.startswith(";"):
                    comments.append(line)
        with gcode_path.open("r", errors="replace") as fh:
            for line in deque(fh, maxlen=4000):
                if line.startswith(";"):
                    comments.append(line)
        text += "\n" + "\n".join(comments)

    m = re.search(r"filament used \[g\]\s*=\s*([\d.,\s]+)", text, re.I)
    if m:
        vals = [float(v) for v in re.findall(r"[\d.]+", m.group(1))]
        if vals:
            filament_g = round(sum(vals), 2)
            if len(vals) > 1:
                by_tool = {f"T{i}": round(v, 2) for i, v in enumerate(vals)}
    if filament_g is None:
        m = re.search(r"filament[^\n:]*?[:=]\s*([\d.]+)\s*g\b", text, re.I)
        if m:
            filament_g = round(float(m.group(1)), 2)

    for pat in (
        r"estimated printing time[^\n=]*=\s*([0-9hms\s.]+)",
        r"total estimated time\s*:\s*([0-9hms\s.]+)",
        r"model printing time\s*:\s*([0-9hms\s.]+)",
        r"print(?:ing)? time[^\n:]*[:=]\s*([0-9hms\s.]+)",
    ):
        m = re.search(pat, text, re.I)
        if m:
            time_s = _parse_hms(m.group(1))
            if time_s:
                break

    return {"time_s": time_s, "filament_g": filament_g, "filament_g_by_tool": by_tool}


# ------------------------------------------------------- 3MF sanitization
#
# Upstream OrcaSlicer 2.4.0 CLI has two hard bugs with foreign 3MFs:
# 1. The presence of <metadata name="Application"> in 3D/3dmodel.model +
#    --load-settings/--load-filaments => SIGSEGV (both fork and upstream).
#    Fix: strip ALL <metadata> elements from 3D/3dmodel.model. This also
#    neutralizes the "File Version X not supported" check (nothing to check).
# 2. A 3MF-embedded project_settings.config triggers -16/-17 printer/process
#    compat checks that cannot pass for foreign (Bambu) 3MFs. Fix: drop
#    project_settings.config entirely; our --load-settings/--load-filaments
#    supply the U1 config (they override 3MF settings per CLI help).
#
# What we keep: geometry + paint data (3D/), package rels, per-object extruder
# mapping (Metadata/model_settings.config), plate defs. The paint survives in
# the triangle attributes; the CLI maps it to our loaded filaments.

_KEEP_PREFIXES = ("3D/", "_rels/")
_KEEP_EXACT = {"[Content_Types].xml"}
_METADATA_RE = re.compile(rb"<metadata\s+name=\"[^\"]*\"\s*/>")
_METADATA_PAIR_RE = re.compile(rb"<metadata\s+name=\"[^\"]*\">.*?</metadata>", re.S)


def sanitize_3mf(src: Path, dest: Path) -> None:
    """Rebuild a 3MF safe for headless slicing.

    Strips the <metadata> block from 3D/3dmodel.model (Application version
    stamp = CLI segfault trigger) and drops Metadata/project_settings.config
    (foreign printer compat checks). Keeps geometry, paint attributes,
    model_settings.config (extruder mapping), and plate JSONs.
    """
    with zipfile.ZipFile(src) as zin:
        names = zin.namelist()
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
            for n in names:
                if n == "Metadata/project_settings.config":
                    continue
                keep = (
                    n in _KEEP_EXACT
                    or n.startswith(_KEEP_PREFIXES)
                    or n == "Metadata/model_settings.config"
                    or re.fullmatch(r"Metadata/plate_\d+\.json", n)
                )
                if not keep:
                    continue
                content = zin.read(n)
                if n == "3D/3dmodel.model":
                    content = _METADATA_RE.sub(b"", content)
                    content = _METADATA_PAIR_RE.sub(b"", content)
                zout.writestr(n, content)


def build_minimal_3mf(src: Path, dest: Path) -> None:
    """Legacy alias: minimal rebuild is now the standard sanitize step."""
    sanitize_3mf(src, dest)


# ------------------------------------------------------- G-code thumbnails
#
# OrcaSlicer's headless CLI never embeds thumbnail blocks: the preview
# renderer only runs in the GUI path, so CLI-sliced G-code has no
# "; thumbnail begin" comments. Consequence: Fluidd shows no file preview
# and the U1 screen falls back to its built-in placeholder (a rabbit).
# Fix: lift the best thumbnail PNG/JPG out of the original 3MF *before*
# sanitizing, then splice standard thumbnail blocks into the sliced G-code.
#
# Two hard-won details (Oct 2026):
# 1. Moonraker's metadata parser checks the header size against the length
#    of the BASE64 STRING (len("".join(b64 lines))), not the decoded byte
#    size. Writing len(png_bytes) makes every block fail the size check and
#    get silently skipped -- no thumbnails anywhere.
# 2. The Snapmaker U1 touchscreen does not use Moonraker's relative_path:
#    its file-selection screen looks for {basename}-96x96.png and its
#    Filament Setup screen looks for {basename}-300x300.png (mirrored by
#    PAXX's Moonraker patch into the root .thumbs dir). So we embed exactly
#    96x96 and 300x300 squares, not aspect-preserved sizes.

_THUMB_SIZES = (300, 96)
_THUMB_BEGIN_RE = re.compile(r"^; thumbnail begin", re.M)


def extract_3mf_thumbnail(src: Path) -> bytes | None:
    """Return the largest embedded PNG/JPG in a 3MF, or None if absent."""
    try:
        with zipfile.ZipFile(src) as z:
            imgs = [n for n in z.namelist()
                    if n.lower().endswith((".png", ".jpg", ".jpeg"))]
            if not imgs:
                return None
            best = max(imgs, key=lambda n: z.getinfo(n).file_size)
            return z.read(best)
    except Exception:
        return None


def _render_thumbnail(img_bytes: bytes, size: int) -> tuple[int, int, bytes] | None:
    """Render an exact size x size PNG; return (w, h, png_bytes).

    The image is fitted inside the square and centered on a background
    sampled from its corners, so the declared WxH always matches the real
    dimensions and the U1 screen's 96x96 / 300x300 slots are hit exactly.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    import io
    from collections import Counter
    try:
        im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        w, h = im.size
        corners = [im.getpixel((0, 0)), im.getpixel((w - 1, 0)),
                   im.getpixel((0, h - 1)), im.getpixel((w - 1, h - 1))]
        bg = Counter(corners).most_common(1)[0][0]
        sq = ImageOps.pad(im, (size, size), method=Image.LANCZOS, color=bg,
                          centering=(0.5, 0.5))
        buf = io.BytesIO()
        sq.save(buf, "PNG")
        return sq.width, sq.height, buf.getvalue()
    except Exception:
        return None


def embed_gcode_thumbnails(gcode_path: Path, img_bytes: bytes | None) -> bool:
    """Splice "; thumbnail begin/end" blocks into G-code. True if embedded."""
    if not img_bytes:
        return False
    blocks = []
    for size in _THUMB_SIZES:
        rendered = _render_thumbnail(img_bytes, size)
        if rendered is None:
            continue
        w, h, png = rendered
        b64 = base64.b64encode(png).decode("ascii")
        # NOTE: the size field must be the base64 *string* length --
        # Moonraker compares it against len("".join(b64 lines)).
        lines = [f"; thumbnail begin {w}x{h} {len(b64)}"]
        lines += ["; " + b64[i:i + 78] for i in range(0, len(b64), 78)]
        lines += ["; thumbnail end", ";"]
        blocks.append("\n".join(lines))
    if not blocks:
        return False
    text = gcode_path.read_text(encoding="utf-8", errors="replace")
    if _THUMB_BEGIN_RE.search(text):
        return False  # already has thumbnails; don't duplicate
    block = "\n".join(blocks) + "\n"
    marker = ";HEADER_BLOCK_END"
    idx = text.find(marker)
    if idx >= 0:
        eol = text.find("\n", idx) + 1
        text = text[:eol] + block + text[eol:]
    else:
        text = block + text
    gcode_path.write_text(text, encoding="utf-8")
    return True


# ------------------------------------------ color-preview point extraction
#
# The confirm screen shows a rendered preview of the sliced model in the
# *mapped* colors (palette slot -> chosen toolhead -> spool color). The
# slicer already resolved paint -> tools, so the ground truth is the G-code
# itself: parse extrusion moves per tool, decimate, and stash compact
# (x, y, z, tool) points. The API renders them on demand (isometric,
# numpy-vectorized) with whatever color mapping the user currently picked.

_PREVIEW_MAX_POINTS = 120_000
_GCODE_T_RE = re.compile(r"^T(\d+)\b")
_GCODE_COORD_RE = re.compile(r"\b([XYZE])(-?\d*\.?\d+)")


def _is_extrusion_move(s: str) -> bool:
    if len(s) < 2 or s[0] != "G" or s[1] not in "01":
        return False
    rest = s[2:]
    if rest and rest[0].isdigit():
        return False
    return "E" in rest


_REMAP_T_RE = re.compile(r"^T(\d+)\b(.*)$")
_REMAP_M104_RE = re.compile(r"^(M10[49]\b.*?)\bT(\d+)\b(.*)$")
_REMAP_EXTRUDE_RE = re.compile(r"^G[01]\b.*\bE(-?\d*\.?\d+)")


def remap_gcode_tools(gcode_path: Path, toolmap: dict[int, int]) -> bool:
    """Rewrite G-code tool selectors per the user's slot->toolhead mapping.

    The picker lets the user assign palette slot N to physical toolhead M,
    but Orca emits positional T-commands (slot N -> TN, which selects
    physical TN). Without a rewrite the printer would ignore the picks and
    always use the positional toolheads. toolmap: {slot: physical}.

    Also forces an explicit T-command before the first extrusion: Orca often
    omits it for the starting tool (assuming the printer's default T0), so
    without this the print would begin on the wrong head even when every
    explicit T-command is remapped.
    Returns True when any line changed.
    """
    remap = {int(k): int(v) for k, v in toolmap.items()}
    if not remap or all(k == v for k, v in remap.items()):
        return False
    try:
        text = gcode_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    changed = False
    out: list[str] = []
    current_tool = 0  # printer powers on / defaults to T0
    t_seen_before_first_extrusion = False
    first_extrusion_done = False
    for line in text.splitlines():
        code, sep, comment = line.partition(";")
        s = code.strip()
        m = _REMAP_T_RE.match(s)
        if m:
            old_t = int(m.group(1))
            if old_t in remap:
                s = f"T{remap[old_t]}" + m.group(2)
                line = s + (sep + comment if sep else "")
                changed = True
            current_tool = old_t
            if not first_extrusion_done:
                t_seen_before_first_extrusion = True
        else:
            m = _REMAP_M104_RE.match(s)
            if m:
                old_t = int(m.group(2))
                if old_t in remap:
                    s = f"{m.group(1)}T{remap[old_t]}" + m.group(3)
                    line = s + (sep + comment if sep else "")
                    changed = True
        # First extrusion with no prior T-command: pin the remapped tool.
        # Always explicit, so the start tool never depends on printer state.
        if not first_extrusion_done and _REMAP_EXTRUDE_RE.match(s):
            first_extrusion_done = True
            if not t_seen_before_first_extrusion:
                out.append(f"T{remap.get(current_tool, current_tool)} ; bridge: pin start tool")
                changed = True
        out.append(line)
    if changed:
        gcode_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return changed


def extract_preview_points(gcode_path: Path, dest: Path) -> bool:
    """Extract decimated (x, y, z, tool) extrusion points; save .npz.

    Two passes: count extrusion moves, then keep every k-th so the point
    budget holds for huge prints. Returns True when points were saved.
    """
    import numpy as np

    total = 0
    with gcode_path.open("r", errors="replace") as fh:
        for line in fh:
            if _is_extrusion_move(line.split(";", 1)[0].strip()):
                total += 1
    if total == 0:
        return False
    stride = max(1, total // _PREVIEW_MAX_POINTS)

    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    ts: list[int] = []
    x = y = z = 0.0
    tool = 0
    e = 0.0
    n = 0
    with gcode_path.open("r", errors="replace") as fh:
        for line in fh:
            s = line.split(";", 1)[0].strip()
            if not s:
                continue
            if s[0] == "T":
                m = _GCODE_T_RE.match(s)
                if m:
                    tool = int(m.group(1)) & 0xFF
                continue
            if s == "G92 E0" or s.startswith("G92 E0 "):
                e = 0.0
                continue
            if not _is_extrusion_move(s):
                if s.startswith("G0") or s.startswith("G1"):
                    m = re.search(r"\bZ(-?\d*\.?\d+)", s)
                    if m:
                        z = float(m.group(1))
                continue
            nx = ny = nz = ne = None
            for axis, val in _GCODE_COORD_RE.findall(s):
                fval = float(val)
                if axis == "X":
                    nx = fval
                elif axis == "Y":
                    ny = fval
                elif axis == "Z":
                    nz = fval
                else:
                    ne = fval
            extruding = ne is not None and ne > e + 1e-9
            if ne is not None:
                e = ne
            if nx is not None:
                x = nx
            if ny is not None:
                y = ny
            if nz is not None:
                z = nz
            if extruding and (nx is not None or ny is not None):
                if n % stride == 0:
                    xs.append(x)
                    ys.append(y)
                    zs.append(z)
                    ts.append(tool)
                n += 1
    if not xs:
        return False
    xyz = np.column_stack(
        [np.array(xs, dtype=np.float32), np.array(ys, dtype=np.float32),
         np.array(zs, dtype=np.float32)])
    np.savez_compressed(str(dest), xyz=xyz, tool=np.array(ts, dtype=np.uint8))
    return True


# ------------------------------------------------- 3MF scale + copies
#
# The Orca CLI has no --scale / --copies flags, so the bridge rewrites the
# 3MF before slicing:
# - scale_3mf: multiplies every vertex in 3D/*.model by (sx, sy, sz).
#   Paint data rides on triangle indices, so colors survive untouched.
# - arrange_copies: clones <build> <item> entries on a centered grid with
#   clearance, so one slice prints N copies. Raises ValueError with a
#   human-readable message when the copies don't fit the 270mm U1 bed.

_BED_X = _BED_Y = _BED_Z = 270.0
_COPY_GAP = 8.0  # mm clearance between copies on the bed

_VERT_RE = re.compile(rb'<vertex\s+x="([^"]+)"\s+y="([^"]+)"\s+z="([^"]+)"(\s*/>)')
_OBJECT_RE = re.compile(rb'<object\s[^>]*\bid="(\d+)"[^>]*>(.*?)</object>', re.S)
_ITEM_RE = re.compile(r'<item\b([^>]*?)/>')
_OBJECTID_RE = re.compile(r'objectid="(\d+)"')
_TRANSFORM_RE = re.compile(r'transform="([^"]+)"')


def scale_3mf(src: Path, dest: Path, sx: float, sy: float, sz: float) -> None:
    """Copy a 3MF scaling all model vertices by (sx, sy, sz)."""
    def _scale(m: re.Match) -> bytes:
        try:
            x, y, z = float(m.group(1)), float(m.group(2)), float(m.group(3))
        except ValueError:
            return m.group(0)
        return (f'<vertex x="{x * sx:.3f}" y="{y * sy:.3f}" '
                f'z="{z * sz:.3f}"{m.group(4).decode()}').encode()

    with zipfile.ZipFile(src) as zin, \
            zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
        for n in zin.namelist():
            data = zin.read(n)
            if n.startswith("3D/") and n.endswith(".model"):
                data = _VERT_RE.sub(_scale, data)
            zout.writestr(n, data)


def _parse_object_bboxes(model_xml: bytes) -> dict[int, tuple]:
    """object id -> (minx, miny, minz, maxx, maxy, maxz) in object space."""
    out: dict[int, tuple] = {}
    for m in _OBJECT_RE.finditer(model_xml):
        oid = int(m.group(1))
        mins = [float("inf")] * 3
        maxs = [float("-inf")] * 3
        n = 0
        for vm in _VERT_RE.finditer(m.group(2)):
            try:
                v = (float(vm.group(1)), float(vm.group(2)), float(vm.group(3)))
            except ValueError:
                continue
            n += 1
            for i in range(3):
                mins[i] = min(mins[i], v[i])
                maxs[i] = max(maxs[i], v[i])
        if n:
            out[oid] = (mins[0], mins[1], mins[2], maxs[0], maxs[1], maxs[2])
    return out


def _parse_transform(s: str | None) -> list[float]:
    if not s:
        return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0]
    vals = [float(x) for x in s.split()]
    if len(vals) != 12:
        raise ValueError(f"bad 3MF transform ({len(vals)} values)")
    return vals


def _transform_bbox(bbox: tuple, t: list[float]) -> tuple:
    """Transform an object-space bbox by a 3MF 3x4 row-major matrix."""
    (x0, y0, z0, x1, y1, z1) = bbox
    pts = []
    for x in (x0, x1):
        for y in (y0, y1):
            for z in (z0, z1):
                pts.append((
                    t[0] * x + t[1] * y + t[2] * z + t[3],
                    t[4] * x + t[5] * y + t[6] * z + t[7],
                    t[8] * x + t[9] * y + t[10] * z + t[11],
                ))
    xs, ys, zs = zip(*pts)
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def arrange_copies(src: Path, dest: Path, copies: int) -> dict:
    """Lay out `copies` duplicates of the 3MF build on a centered grid.

    Returns {"cols", "rows", "footprint": [w, d]}. Raises ValueError when the
    copies cannot fit the bed.
    """
    with zipfile.ZipFile(src) as zin:
        names = zin.namelist()
        others = {n: zin.read(n) for n in names if n != "3D/3dmodel.model"}
        model_xml = zin.read("3D/3dmodel.model")

    bboxes: dict[int, tuple] = {}
    with zipfile.ZipFile(src) as zin:
        for n in names:
            if n.startswith("3D/") and n.endswith(".model"):
                bboxes.update(_parse_object_bboxes(zin.read(n)))

    build_m = re.search(r"<build>(.*?)</build>", model_xml.decode("utf-8", "replace"), re.S)
    if not build_m:
        raise ValueError("3MF has no <build> section; cannot duplicate items.")
    items = []
    for im in _ITEM_RE.finditer(build_m.group(1)):
        attrs = im.group(1)
        oid_m = _OBJECTID_RE.search(attrs)
        if not oid_m:
            continue
        t_m = _TRANSFORM_RE.search(attrs)
        items.append((im.group(0), int(oid_m.group(1)),
                      _parse_transform(t_m.group(1) if t_m else None)))
    if not items:
        raise ValueError("3MF build has no printable items.")
    missing = [oid for _, oid, _ in items if oid not in bboxes]
    if missing:
        raise ValueError(f"3MF references missing object(s) {missing}; cannot lay out copies.")

    # Union bbox of the whole build in build space.
    ux0, uy0, uz0 = float("inf"), float("inf"), float("inf")
    ux1, uy1, uz1 = float("-inf"), float("-inf"), float("-inf")
    for _, oid, t in items:
        x0, y0, z0, x1, y1, z1 = _transform_bbox(bboxes[oid], t)
        ux0, uy0, uz0 = min(ux0, x0), min(uy0, y0), min(uz0, z0)
        ux1, uy1, uz1 = max(ux1, x1), max(uy1, y1), max(uz1, z1)
    w, d, h = ux1 - ux0, uy1 - uy0, uz1 - uz0
    if h > _BED_Z:
        raise ValueError(f"Model is {h:.0f}mm tall after scaling; the U1 fits {_BED_Z:.0f}mm.")

    info = {"cols": 1, "rows": 1, "footprint": [round(w, 1), round(d, 1)]}
    new_items_xml = ""
    if copies > 1:
        if w > _BED_X or d > _BED_Y:
            raise ValueError(
                f"Model footprint is {w:.0f}x{d:.0f}mm; it doesn't fit the "
                f"{_BED_X:.0f}x{_BED_Y:.0f}mm bed.")
        max_cols = max(1, int((_BED_X + _COPY_GAP) // (w + _COPY_GAP)))
        max_rows = max(1, int((_BED_Y + _COPY_GAP) // (d + _COPY_GAP)))
        # Most-square grid that fits: minimize the long side, then imbalance.
        best = None
        for cc in range(1, min(copies, max_cols) + 1):
            rr = (copies + cc - 1) // cc
            if rr > max_rows:
                continue
            key = (max(cc, rr), abs(cc - rr), -cc)
            if best is None or key < best[0]:
                best = (key, cc, rr)
        if best is None:
            raise ValueError(
                f"Only {max_cols * max_rows} copie(s) of this size fit on the bed; "
                f"asked for {copies}.")
        _, cols, rows = best
        info.update(cols=cols, rows=rows)
        # Center the grid on the original build position: every copy (the
        # first included) is re-emitted at its grid cell, translated from the
        # original union center.
        ucx, ucy = (ux0 + ux1) / 2, (uy0 + uy1) / 2
        grid_w = cols * w + (cols - 1) * _COPY_GAP
        grid_d = rows * d + (rows - 1) * _COPY_GAP
        for k in range(copies):
            c, r = k % cols, k // cols
            cell_cx = ucx + c * (w + _COPY_GAP) + w / 2 - grid_w / 2
            cell_cy = ucy + r * (d + _COPY_GAP) + d / 2 - grid_d / 2
            tx, ty = cell_cx - ucx, cell_cy - ucy
            for xml, oid, t in items:
                nt = list(t)
                nt[3] += tx  # pre-multiplying by a translation shifts t
                nt[7] += ty
                t_str = " ".join(f"{v:.3f}" for v in nt)
                if _TRANSFORM_RE.search(xml):
                    new_xml = _TRANSFORM_RE.sub(f'transform="{t_str}"', xml)
                else:  # item had no transform attribute (identity)
                    new_xml = xml[:-2].rstrip() + f' transform="{t_str}"/>'
                new_items_xml += new_xml

    # splice new items into the build section (for copies > 1 the grid
    # replaces the original items; for a single copy the build is untouched)
    def _splice(m: re.Match) -> str:
        inner = new_items_xml if copies > 1 else m.group(1)
        return "<build>" + inner + "</build>"

    new_model = re.sub(r"<build>(.*?)</build>",
                       _splice, model_xml.decode("utf-8", "replace"),
                       count=1, flags=re.S).encode()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
        for n, data in others.items():
            zout.writestr(n, data)
        zout.writestr("3D/3dmodel.model", new_model)
    return info


# ------------------------------------------------------------------ slicing

def run_slice(job_dir, input_path, plate, filament_paths, process_profile=None):
    """Run Orca headless. Returns (ok, combined_output, gcode_path|None, returncode)."""
    outdir = job_dir / "sliced"
    outdir.mkdir(exist_ok=True)
    proc_profile = process_profile or PROCESS_PROFILE
    cmd = [
        "xvfb-run", "-a", ORCA_BIN,
        "--allow-newer-file",  # tolerate 3MFs saved by newer slicers
        "--slice", str(plate + 1),  # job plate is 0-based; CLI is 1-based
        "--load-settings", f"{MACHINE_PROFILE};{proc_profile}",
        "--load-filaments", ";".join(filament_paths),
        "--outputdir", str(outdir),
        str(input_path),
    ]
    log.info("slicing: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0:
        return False, combined, None, proc.returncode
    gcodes = sorted(outdir.glob("plate_*.gcode"), key=lambda p: p.stat().st_mtime)
    gcode = gcodes[-1] if gcodes else None
    if gcode is None:
        return False, combined + "\n[bridge] CLI exited 0 but no plate_*.gcode was produced", None, proc.returncode
    return True, combined, gcode, proc.returncode


def process_job(conn: sqlite3.Connection, job_id: str) -> None:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None or row["status"] != "slicing":
        _set(conn, job_id, claimed_by=None)
        return
    job_dir = DATA_DIR / "jobs" / job_id
    plan = json.loads(row["plan_json"] or "[]")
    slot_spools = json.loads(row["slot_spools_json"] or "{}")
    input_path = Path(row["input_path"])
    plate = row["plate"] or 0

    _append_log(conn, job_id, "info",
                f"Slicing plate {plate + 1} of {row['plates']} on the U1 profiles...")
    quality = (row["quality"] or "standard")
    _append_log(conn, job_id, "info", f"Quality preset: {quality}.")
    try:
        filament_paths = build_filament_profiles(job_dir, plan, slot_spools)
    except Exception as exc:
        _fail(conn, job_id, f"Could not build filament profiles: {exc}", "slice_failed")
        return

    # Sanitize 3MFs up front: foreign (Bambu/MakerWorld) 3MFs carry an
    # Application version stamp and printer-specific project config that
    # crash or block the headless CLI. Sanitizing keeps geometry + paint.
    # Grab the 3MF's own thumbnail first -- the CLI never embeds previews,
    # so we splice it into the G-code ourselves after slicing.
    thumb_bytes = None
    if input_path.suffix.lower() == ".3mf":
        thumb_bytes = extract_3mf_thumbnail(input_path)
        if thumb_bytes is None:
            _append_log(conn, job_id, "info",
                        "No thumbnail found in 3MF; print preview will be unavailable.")
    if input_path.suffix.lower() == ".3mf":
        try:
            clean = job_dir / "model_sanitized.3mf"
            sanitize_3mf(input_path, clean)
            input_path = clean
            _set(conn, job_id, input_path=str(clean))
            _append_log(conn, job_id, "info",
                        "Sanitized 3MF (stripped foreign metadata/project config).")
        except Exception as exc:
            _append_log(conn, job_id, "warning",
                        f"3MF sanitization failed ({exc}); trying original.")

    # Scale then duplicate (copies are laid out from the scaled footprint).
    try:
        scale = {"x": 1.0, "y": 1.0, "z": 1.0}
        try:
            scale.update({k: float(v) for k, v in
                          json.loads(row["scale_json"] or "{}").items()
                          if k in scale})
        except (TypeError, ValueError, AttributeError):
            pass
        copies = row["copies"] or 1
    except Exception:
        scale, copies = {"x": 1.0, "y": 1.0, "z": 1.0}, 1
    if input_path.suffix.lower() == ".3mf":
        try:
            if any(abs(scale[a] - 1.0) > 1e-9 for a in ("x", "y", "z")):
                scaled = job_dir / "model_scaled.3mf"
                scale_3mf(input_path, scaled, scale["x"], scale["y"], scale["z"])
                input_path = scaled
                _set(conn, job_id, input_path=str(scaled))
                _append_log(conn, job_id, "info",
                            f"Scaled model to {scale['x']:.0%} x {scale['y']:.0%} x {scale['z']:.0%}.")
            if copies > 1:
                duped = job_dir / "model_copies.3mf"
                info = arrange_copies(input_path, duped, int(copies))
                input_path = duped
                _set(conn, job_id, input_path=str(duped))
                _append_log(conn, job_id, "info",
                            f"Arranged {copies} copies "
                            f"({info['cols']}x{info['rows']} grid).")
        except Exception as exc:
            _fail(conn, job_id, f"Could not prepare model for slicing: {exc}",
                  "slice_failed")
            return

    supports_mode = None
    try:
        supports_mode = row["supports_mode"]
    except (IndexError, KeyError):
        pass
    if (supports_mode or "model") != "model":
        _append_log(conn, job_id, "info",
                    f"Supports override: {supports_mode} (model file default ignored).")
    try:
        proc_profile = process_profile_with_supports(job_dir, row["quality"], supports_mode)
    except Exception as exc:
        _fail(conn, job_id, f"Could not apply supports setting: {exc}", "slice_failed")
        return

    ok, output, gcode, returncode = run_slice(
        job_dir, input_path, plate, filament_paths,
        process_profile=proc_profile)

    # (3MFs are sanitized up front; no retry path remains.)

    # Job may have been cancelled while slicing -- don't clobber that.
    # NOTE: use a separate variable; `row` still holds the full job record
    # (including slot_toolheads_json) fetched at the start of process_job.
    status_row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    if status_row is None or status_row["status"] != "slicing":
        _set(conn, job_id, claimed_by=None)
        return

    if ok and gcode is not None:
        try:
            toolmap = json.loads(row["slot_toolheads_json"] or "{}")
        except (ValueError, TypeError, IndexError, KeyError):
            toolmap = {}
        # Multi-slot job with no mapping: fail loudly instead of silently
        # printing positional (wrong) tools.
        try:
            n_slots = len(json.loads(row["palette_json"] or "[]"))
        except (ValueError, TypeError):
            n_slots = 0
        if n_slots > 1 and not toolmap:
            _fail(conn, job_id,
                  "Toolhead mapping missing for multi-color job; "
                  "please hard-refresh the page and confirm again.",
                  "slice_failed")
            return
        try:
            if remap_gcode_tools(gcode, toolmap):
                _append_log(conn, job_id, "info",
                            f"Remapped tools per your picks: {toolmap}.")
            else:
                _append_log(conn, job_id, "info",
                            f"Tool mapping is positional {toolmap}; no G-code changes needed.")
        except Exception as exc:
            _fail(conn, job_id, f"Tool remap failed ({exc}).", "slice_failed")
            return
        if thumb_bytes and embed_gcode_thumbnails(gcode, thumb_bytes):
            _append_log(conn, job_id, "info",
                        "Embedded model preview thumbnails into G-code.")
        estimates = parse_estimates(output, gcode)
        try:
            if extract_preview_points(gcode, job_dir / "preview_points.npz"):
                _append_log(conn, job_id, "info", "Built color-preview data.")
        except Exception as exc:
            _append_log(conn, job_id, "warning",
                        f"Color preview unavailable: {exc}")
        _set(conn, job_id, status="uploading", gcode_path=str(gcode),
             estimates_json=json.dumps(estimates), claimed_by=None)
        eta = estimates["time_s"]
        _append_log(conn, job_id, "info",
                    f"Sliced OK: {gcode.name} "
                    f"({estimates['filament_g']} g filament"
                    f"{f', ~{int(eta // 60)} min' if eta else ''}). Handing to uploader.")
    else:
        # Verbose failure: exit code + a generous tail of CLI output go into
        # the job log (visible under Details, copyable via "Copy log") so
        # there's enough context to troubleshoot without container access.
        tail = output[-8000:] if len(output) > 8000 else output
        _fail(conn, job_id,
              f"Slicing failed (CLI exit {returncode}):\n{tail}", "slice_failed")


def _fail(conn: sqlite3.Connection, job_id: str, error: str, code: str) -> None:
    _set(conn, job_id, status="failed", error=error, error_code=code, claimed_by=None)
    _append_log(conn, job_id, "error", error)
    log.warning("job %s failed [%s]", job_id, code)


def main() -> None:
    for name, path in (("machine profile", MACHINE_PROFILE), ("process profile", PROCESS_PROFILE),
                       ("orca binary", ORCA_BIN)):
        if not Path(path).exists():
            log.error("missing %s: %s -- check the Docker build / env", name, path)
    if not DB_PATH.is_file():
        log.error("jobs DB not found at %s; is DATA_DIR shared with the backend?", DB_PATH)
    log.info("slicer worker started (db=%s)", DB_PATH)
    while True:
        try:
            conn = _db()
            try:
                job_id = _claim_next(conn)
                if job_id:
                    log.info("claimed job %s", job_id)
                    try:
                        process_job(conn, job_id)
                    except Exception as exc:
                        log.exception("slice crashed for job %s", job_id)
                        try:
                            _fail(conn, job_id,
                                  f"Internal slicer error ({type(exc).__name__}: {exc}).",
                                  "internal")
                        except Exception:
                            pass
            finally:
                conn.close()
        except Exception:
            log.exception("worker loop error")
        time.sleep(3)


if __name__ == "__main__":
    main()
