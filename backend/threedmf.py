"""Parse-only 3MF/STL inspection for the U1 print bridge.

CRITICAL DESIGN DECISION (positional slot mapping):
Bambu/Orca/Snapmaker-U1 brush-paint data is a proprietary nibble-packed binary
tree (reverse-engineered by the MIT-licensed 3mf-to-glb project; the encoding
is identical across Bambu/Orca/U1). Rather than decode and rewrite that binary,
the bridge maps 3MF filament slots POSITIONALLY to toolheads:

    palette slot 0 (filament_colour[0]) -> T0
    palette slot 1 (filament_colour[1]) -> T1
    ...

The 3MF is passed to the slicer UNMODIFIED. The confirm screen therefore guides
physical spool placement ("load red PLA into T1"), verified live against
Spoolman. Consequences:

1. This module is PARSE-ONLY: it extracts the palette (in slot order),
   per-object extruder indices (for info/validation only), plate count, and a
   thumbnail. It never modifies the 3MF.
2. The Orca CLI crash risk from foreign 3MF configs is lower since the
   original file is kept, but the slicer worker still retries once with a
   minimal 3MF if the CLI crashes.
3. Models using virtual/mixed extruders -- extruder index > 4 in
   Metadata/model_settings.config (e.g. U1 Full Spectrum blends, which use
   virtual extruders 5..16 for mixed colors) -- cannot be mapped to the 4
   physical toolheads and are flagged UNSUPPORTED with a clear error here.

Grounded in real samples (/tmp/u1-3mf-samples, verified 2026-10-09):
- Metadata/project_settings.config is JSON with filament_colour (list of
  '#RRGGBBAA' or '#RRGGBB') and filament_type (list of material names),
  slot order == toolhead order.
- Metadata/model_settings.config is XML; <object>/<part> elements carry
  <metadata key="extruder" value="N"/> (1-based extruder index).
- Plates: Metadata/plate_N.json (fallback: Metadata/plate_N.png).
- Thumbnail: Metadata/plate_1.png.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
from dataclasses import dataclass, field
from pathlib import Path

from .colors import normalize_hex

PROJECT_SETTINGS = "Metadata/project_settings.config"
MODEL_SETTINGS = "Metadata/model_settings.config"
PLATE_JSON_RE = re.compile(r"^Metadata/plate_(\d+)\.json$")
PLATE_PNG_RE = re.compile(r"^Metadata/plate_(\d+)\.png$")
EXTRUDER_RE = re.compile(r'<metadata\s+key="extruder"\s+value="(\d+)"')
_ASCII_VERTEX_RE = re.compile(rb"^\s*vertex\s+(\S+)\s+(\S+)\s+(\S+)", re.M)

# Largest single ZIP member the inspector will unpack (200 MB). 3MF model
# XML is a few MB at most; the cap stops a zip bomb (many GB of vertices
# behind a small compressed file) from exhausting memory. ZipInfo.file_size
# is the *decompressed* size, checked before any read.
MAX_ZIP_MEMBER_BYTES = 200 * 1024 * 1024


def _read_zip_member(zf: zipfile.ZipFile, name: str,
                     max_bytes: int = MAX_ZIP_MEMBER_BYTES) -> bytes:
    """Read one ZIP member, refusing entries whose declared uncompressed
    size exceeds max_bytes. Raises InspectorError (user-facing)."""
    info = zf.getinfo(name)
    if info.file_size > max_bytes:
        raise InspectorError(
            f"3MF member {name!r} is {info.file_size / (1024 * 1024):.0f} MB "
            f"(limit {max_bytes // (1024 * 1024)} MB) -- refusing to unpack it.")
    return zf.read(name)


def _local(tag) -> str:
    """Element tag without its namespace: '{ns}vertex' -> 'vertex'."""
    return tag.split("}", 1)[-1] if isinstance(tag, str) else ""


def _parse_model_xml(data: bytes) -> ET.Element:
    """Parse a 3D/*.model document with ElementTree.

    Elements are matched by local name (namespace/prefix-independent) and
    attributes are read by name, so vertex parsing no longer depends on
    attribute order (the old regex required x=".." y=".." z=".." exactly).
    DTDs are rejected outright: std-lib ET does not expand external
    entities, but a DTD with internal entities enables billion-laughs
    expansion on an untrusted model file.
    """
    if b"<!doctype" in data.lower():
        raise ValueError("3MF model XML contains a DOCTYPE")
    return ET.fromstring(data)

# U1 build volume (mm). Used to sanity-check scale/copy requests.
BED_X, BED_Y, BED_Z = 270.0, 270.0, 270.0

# U1 has 4 physical toolheads; extruder indices in model_settings.config are
# 1-based, so 1..4 map to T0..T3. Anything above is a virtual/mixed extruder.
MAX_PHYSICAL_EXTRUDER = 4


class InspectorError(Exception):
    """User-friendly inspection failure. `code` drives the API error_code."""

    def __init__(self, message: str, code: str = "inspect_failed"):
        super().__init__(message)
        self.code = code


@dataclass
class Inspection:
    kind: str  # "3mf" or "stl"
    palette: list[dict] = field(default_factory=list)  # [{slot, hex, material}]
    extruders: list[int] = field(default_factory=list)  # 1-based, sorted, unique
    plates: int = 1
    thumbnail_bytes: bytes | None = None
    title: str | None = None
    dimensions: dict | None = None  # {"x","y","z"} model bbox in mm, if measurable
    volumes: dict[int, float] = field(default_factory=dict)  # 1-based extruder -> cm^3


_OBJECT_EXTRUDER_RE = re.compile(
    rb'<object\b[^>]*\bid="(\d+)"[^>]*>.*?<metadata\s+key="extruder"\s+value="(\d+)"',
    re.S)


def estimate_volumes(path: str | Path) -> dict[int, float]:
    """Per-extruder material volume (cm^3) from 3MF mesh geometry.

    Returns {1-based extruder index: cm^3}. Objects without extruder metadata
    count toward extruder 1. Signed volumes are abs()'d per object so flipped
    winding can't cancel real geometry out. Used for pre-slice filament
    estimates in the confirm screen; the post-slice G-code parse remains the
    authority.
    """
    vols: dict[int, float] = {}
    try:
        zf = zipfile.ZipFile(path)
    except Exception:
        return vols
    with zf:
        names = zf.namelist()
        model_name = next((n for n in names if n.lower().endswith("3dmodel.model")), None)
        if not model_name:
            model_name = next((n for n in names if n.startswith("3D/") and n.endswith(".model")), None)
        if not model_name:
            return vols
        try:
            data = _read_zip_member(zf, model_name)
            root = _parse_model_xml(data)
        except (KeyError, ValueError, InspectorError):
            return vols
        # object id -> extruder (1-based) from Bambu/Orca model settings
        extruder_by_object: dict[int, int] = {}
        if MODEL_SETTINGS in names:
            try:
                cfg = _read_zip_member(zf, MODEL_SETTINGS)
                for m in _OBJECT_EXTRUDER_RE.finditer(cfg):
                    extruder_by_object[int(m.group(1))] = int(m.group(2))
            except (KeyError, InspectorError):
                pass
        for obj in root.iter():
            if _local(obj.tag) != "object":
                continue
            try:
                oid = int(obj.get("id"))
            except (TypeError, ValueError):
                continue
            verts: list[tuple[float, float, float]] = []
            tris: list[tuple[int, int, int]] = []
            for el in obj.iter():
                tag = _local(el.tag)
                if tag == "vertex":
                    try:
                        verts.append((float(el.get("x")), float(el.get("y")),
                                      float(el.get("z"))))
                    except (TypeError, ValueError):
                        continue
                elif tag == "triangle":
                    try:
                        tris.append((int(el.get("v1")), int(el.get("v2")),
                                     int(el.get("v3"))))
                    except (TypeError, ValueError):
                        continue
            if not verts or not tris:
                continue
            try:
                v = np.asarray(verts, dtype=np.float64)
                t = np.asarray(tris, dtype=np.int64)
                if t.max() >= len(v):
                    continue
                p0, p1, p2 = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
                vol_mm3 = abs(float(np.sum(np.einsum("ij,ij->i", p0, np.cross(p1, p2))) / 6.0))
            except Exception:
                continue
            ext = extruder_by_object.get(oid, 1)
            vols[ext] = vols.get(ext, 0.0) + vol_mm3 / 1000.0
    return vols


def _plate_count(names: list[str]) -> int:
    nums = {int(m.group(1)) for n in names if (m := PLATE_JSON_RE.match(n))}
    if not nums:
        nums = {int(m.group(1)) for n in names if (m := PLATE_PNG_RE.match(n))}
    return max(nums) if nums else 1


def _parse_palette(project_settings: bytes) -> list[dict]:
    try:
        cfg = json.loads(project_settings)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InspectorError(f"Could not read the 3MF project settings ({exc}).") from exc
    colours = cfg.get("filament_colour") or []
    types = cfg.get("filament_type") or []
    palette = []
    for i, raw in enumerate(colours):
        hex6 = normalize_hex(raw if isinstance(raw, str) else None)
        material = types[i] if i < len(types) and isinstance(types[i], str) else None
        palette.append({"slot": i, "hex": hex6, "material": material})
    return palette


def _parse_extruders(model_settings_xml: str) -> list[int]:
    return sorted({int(v) for v in EXTRUDER_RE.findall(model_settings_xml)})


def _bbox_from_points(pts: list[tuple[float, float, float]]) -> dict | None:
    if not pts:
        return None
    xs, ys, zs = zip(*pts)
    return {
        "x": round(max(xs) - min(xs), 1),
        "y": round(max(ys) - min(ys), 1),
        "z": round(max(zs) - min(zs), 1),
    }


def _bbox_3mf(zf: zipfile.ZipFile) -> dict | None:
    """Union bounding-box size of all 3D/*.model vertices (object space).

    A model file that fails to parse is skipped (dimensions are advisory;
    the slicer is the authority) rather than failing the inspection.
    """
    pts: list[tuple[float, float, float]] = []
    for name in zf.namelist():
        if not (name.startswith("3D/") and name.endswith(".model")):
            continue
        try:
            data = _read_zip_member(zf, name)
            root = _parse_model_xml(data)
        except (KeyError, ValueError, InspectorError):
            continue
        for el in root.iter():
            if _local(el.tag) != "vertex":
                continue
            try:
                pts.append((float(el.get("x")), float(el.get("y")), float(el.get("z"))))
            except (TypeError, ValueError):
                continue
    return _bbox_from_points(pts)


def _bbox_stl(path: Path) -> dict | None:
    """Bounding-box size of a binary or ASCII STL."""
    import struct

    data = path.read_bytes()
    pts: list[tuple[float, float, float]] = []
    if len(data) > 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if 84 + count * 50 == len(data):
            # binary STL: 50-byte records, verts are floats 3..11
            for i in range(count):
                off = 84 + i * 50 + 12
                vals = struct.unpack_from("<9f", data, off)
                pts.extend([(vals[0], vals[1], vals[2]),
                            (vals[3], vals[4], vals[5]),
                            (vals[6], vals[7], vals[8])])
            return _bbox_from_points(pts)
    # ASCII fallback
    for m in _ASCII_VERTEX_RE.finditer(data):
        try:
            pts.append((float(m.group(1)), float(m.group(2)), float(m.group(3))))
        except ValueError:
            continue
    return _bbox_from_points(pts)


def _check_extruders(extruders: list[int]) -> None:
    virtual = [e for e in extruders if e > MAX_PHYSICAL_EXTRUDER]
    if virtual:
        raise InspectorError(
            "This model uses virtual/mixed extruders "
            f"(extruder indices {sorted(virtual)}), e.g. a Full Spectrum color "
            "blend, which needs more than the U1's 4 physical toolheads. "
            "The bridge can't map these, so this model isn't supported. "
            "Slice it manually in OrcaSlicer if you still want to print it.",
            code="unsupported",
        )


def _single_slot_palette() -> list[dict]:
    # "As modeled" path: STL input or a bare 3MF export with no project
    # settings. One slot, printed on T0 in whatever is loaded there.
    return [{"slot": 0, "hex": None, "material": None}]


def inspect(path: str | Path) -> Inspection:
    """Inspect a .3mf or .stl file. Raises InspectorError on any problem."""
    path = Path(path)
    if not path.is_file():
        raise InspectorError(f"File not found: {path}")
    suffix = path.suffix.lower()

    if suffix in (".stl", ".obj"):
        # Single unpainted mesh: one slot, printed on T0 ("as modeled").
        dims = _bbox_stl(path) if suffix == ".stl" else None
        return Inspection(kind="stl", palette=_single_slot_palette(), plates=1,
                          dimensions=dims)

    if suffix != ".3mf":
        raise InspectorError(f"Unsupported file type {path.suffix!r}: expected .3mf, .stl or .obj.")

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise InspectorError("That file isn't a valid 3MF (not a ZIP archive).") from exc

    with zf:
        names = zf.namelist()
        plates = _plate_count(names)
        dimensions = _bbox_3mf(zf)

        thumbnail = None
        if "Metadata/plate_1.png" in names:
            thumbnail = _read_zip_member(zf, "Metadata/plate_1.png")

        if PROJECT_SETTINGS not in names:
            # Bare export (e.g. exported from CAD): single-color "as modeled".
            return Inspection(
                kind="3mf",
                palette=_single_slot_palette(),
                plates=plates,
                thumbnail_bytes=thumbnail,
                dimensions=dimensions,
            )

        palette = _parse_palette(_read_zip_member(zf, PROJECT_SETTINGS))

        extruders: list[int] = []
        if MODEL_SETTINGS in names:
            xml = _read_zip_member(zf, MODEL_SETTINGS).decode("utf-8", "replace")
            extruders = _parse_extruders(xml)
            _check_extruders(extruders)  # raises for virtual/mixed extruders

        if not palette:
            # Project settings present but no palette: treat as single slot.
            palette = _single_slot_palette()

    try:
        volumes = estimate_volumes(path)
    except Exception:
        volumes = {}

    return Inspection(
        kind="3mf",
        palette=palette,
        extruders=extruders,
        plates=plates,
        thumbnail_bytes=thumbnail,
        dimensions=dimensions,
        volumes=volumes,
    )
