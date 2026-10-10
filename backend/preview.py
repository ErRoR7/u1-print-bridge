"""Color-preview: G-code point extraction, isometric rendering, estimate backfill.

The confirm screen shows the sliced model in the *mapped* colors. The slicer
already resolved paint -> tools, so the ground truth is the G-code: this
module extracts decimated (x, y, z, tool) extrusion points and renders them
isometrically with whatever color mapping the user picked.

The slicer worker pre-generates preview_points.npz at slice time (see
slicer/worker.py, which carries its own copy of the extractor because the
two Docker build contexts are separate). The API generates it on demand when
missing so older jobs get previews too.
"""
from __future__ import annotations

import re
from pathlib import Path

_PREVIEW_MAX_POINTS = 120_000
_T_RE = re.compile(r"^T(\d+)\b")
_COORD_RE = re.compile(r"\b([XYZE])(-?\d*\.?\d+)")
_TIME_RES = (
    r"estimated printing time[^\n=]*=\s*([0-9hms\s.]+)",
    r"total estimated time\s*:\s*([0-9hms\s.]+)",
    r"model printing time\s*:\s*([0-9hms\s.]+)",
    r"print(?:ing)? time[^\n:]*[:=]\s*([0-9hms\s.]+)",
)
_HMS_RE = re.compile(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:([\d.]+)\s*s)?", re.I)


def _is_extrusion_move(s: str) -> bool:
    if len(s) < 2 or s[0] != "G" or s[1] not in "01":
        return False
    rest = s[2:]
    if rest and rest[0].isdigit():
        return False
    return "E" in rest


def extract_preview_points(gcode_path: Path, dest: Path) -> bool:
    """Extract decimated (x, y, z, tool) extrusion points; save .npz.

    Two passes: count extrusion moves, then keep every k-th so the point
    budget holds for huge prints. Returns True when points were saved.
    (Mirrors slicer/worker.py::extract_preview_points; keep in sync.)
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
                m = _T_RE.match(s)
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
            for axis, val in _COORD_RE.findall(s):
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


def ensure_preview_points(gcode_path: Path, dest: Path) -> bool:
    """Return True when a usable preview_points.npz exists (generating it)."""
    if dest.is_file() and dest.stat().st_size > 0:
        return True
    try:
        return extract_preview_points(gcode_path, dest)
    except Exception:
        return False


def _parse_hms(text: str) -> float | None:
    m = _HMS_RE.search(text or "")
    if not m or not m.group(0).strip():
        return None
    h, mi, s = m.group(1), m.group(2), m.group(3)
    if not (h or mi or s):
        return None
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)


def backfill_time_estimate(gcode_path: Path) -> float | None:
    """Best-effort slicer time estimate (seconds) from G-code comments.

    Scans the head AND tail: OrcaSlicer puts the time estimate in the footer.
    Used to backfill jobs sliced before the parser covered the footer.
    """
    from collections import deque

    head: list[str] = []
    try:
        with gcode_path.open("r", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= 4000:
                    break
                if line.startswith(";"):
                    head.append(line)
        with gcode_path.open("r", errors="replace") as fh:
            tail = [ln for ln in deque(fh, maxlen=4000) if ln.startswith(";")]
    except OSError:
        return None
    text = "\n".join(head + tail)
    for pat in _TIME_RES:
        m = re.search(pat, text, re.I)
        if m:
            secs = _parse_hms(m.group(1))
            if secs:
                return secs
    return None


def render_preview_png(xyz, tools, color_by_tool: dict[int, tuple[int, int, int]],
                       size: int = 600) -> bytes:
    """Isometric render of points in the mapped colors. Returns PNG bytes."""
    import io

    import numpy as np
    from PIL import Image

    pts = np.asarray(xyz, dtype=np.float64)
    c30, s30 = np.cos(np.radians(30.0)), np.sin(np.radians(30.0))
    sx = (pts[:, 0] - pts[:, 1]) * c30
    sy = (pts[:, 0] + pts[:, 1]) * s30 - pts[:, 2]
    pad = 14.0
    rx = float(sx.max() - sx.min()) or 1.0
    ry = float(sy.max() - sy.min()) or 1.0
    sc = min((size - 2 * pad) / rx, (size - 2 * pad) / ry)
    px = ((sx - sx.min()) * sc + pad).astype(np.int64)
    py = ((sy - sy.min()) * sc + pad).astype(np.int64)
    # Viewer looks from (+,+,+): draw far (small x+y+z) first, near overwrites.
    order = np.argsort(pts[:, 0] + pts[:, 1] + pts[:, 2])
    img = np.zeros((size, size, 4), dtype=np.uint8)
    rgb = np.full((len(pts), 3), 128, dtype=np.uint8)
    tarr = np.asarray(tools, dtype=np.int64)
    for t, col in color_by_tool.items():
        rgb[tarr == t] = col
    ro = rgb[order]
    for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
        xx = np.clip(px[order] + dx, 0, size - 1)
        yy = np.clip(py[order] + dy, 0, size - 1)
        img[yy, xx, 0:3] = ro
        img[yy, xx, 3] = 255
    buf = io.BytesIO()
    Image.fromarray(img, "RGBA").save(buf, "PNG")
    return buf.getvalue()
