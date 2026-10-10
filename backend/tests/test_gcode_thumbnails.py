"""G-code thumbnail embedding must satisfy Moonraker's parser and the
U1 screen's expected sizes (96x96, 300x300)."""
import base64
import io
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "slicer"))
import worker as sw

PIL = pytest.importorskip("PIL.Image", reason="Pillow required")


def _png(w=400, h=300, color=(200, 30, 30)):
    im = PIL.new("RGB", (w, h), color)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _moonraker_parse(text):
    """Mirror moonraker/components/file_manager/metadata.py::parse_thumbnails."""
    pattern = r"(thumbnail(?:_[A-Za-z0-9]+)?) begin([;/\+=\w\s]+?); \1 end"
    out = []
    for match in re.finditer(pattern, text):
        lines = re.split(r"\r?\n", match.group(2).replace("; ", ""))
        info = [int(x) for x in re.findall(r"([0-9]+)", lines[0])]
        data = "".join(lines[1:-1])
        assert len(info) == 3, f"bad header: {lines[0]}"
        # Moonraker compares the header size to the base64 *string* length.
        assert len(data) == info[2], (
            f"size mismatch: header says {info[2]}, base64 length is {len(data)}")
        raw = base64.b64decode(data.encode())
        img = PIL.open(io.BytesIO(raw))
        assert img.size == (info[0], info[1])
        out.append((info[0], info[1]))
    return out


def test_embed_thumbnails_u1_sizes_and_moonraker_contract(tmp_path):
    g = tmp_path / "test.gcode"
    g.write_text(";HEADER_BLOCK_END\nG1 X10\n")
    assert sw.embed_gcode_thumbnails(g, _png())
    dims = _moonraker_parse(g.read_text())
    assert sorted(dims) == [(96, 96), (300, 300)]


def test_embed_thumbnails_no_source_noop(tmp_path):
    g = tmp_path / "test.gcode"
    g.write_text("G1 X10\n")
    assert sw.embed_gcode_thumbnails(g, None) is False
    assert sw.embed_gcode_thumbnails(g, b"") is False


def test_embed_thumbnails_idempotent_skip(tmp_path):
    g = tmp_path / "test.gcode"
    g.write_text("; thumbnail begin 300x300 10\n; abc\n; thumbnail end\nG1\n")
    assert sw.embed_gcode_thumbnails(g, _png()) is False


def _synth_gcode(path: Path):
    lines = ["; synth", "T0", "G92 E0"]
    e = 0.0
    for y in range(0, 50, 5):
        for x in range(0, 50, 5):
            e += 1.0
            lines.append(f"G1 X{x} Y{y} Z0.2 E{e:.2f}")
    lines += ["T1", "G92 E0"]
    e = 0.0
    for y in range(0, 50, 5):
        for x in range(50, 100, 5):
            e += 1.0
            lines.append(f"G1 X{x} Y{y} Z0.2 E{e:.2f}")
    path.write_text("\n".join(lines) + "\n")


def test_extract_preview_points_tools(tmp_path):
    import numpy as np
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "slicer"))
    import worker as sw
    g = tmp_path / "s.gcode"
    _synth_gcode(g)
    dest = tmp_path / "p.npz"
    assert sw.extract_preview_points(g, dest)
    d = np.load(str(dest))
    assert set(d["tool"].tolist()) == {0, 1}
    assert d["xyz"].shape[1] == 3
    # T0 points are left (x<50), T1 right
    t0x = d["xyz"][d["tool"] == 0][:, 0]
    t1x = d["xyz"][d["tool"] == 1][:, 0]
    assert t0x.max() < 50 and t1x.min() >= 50


def test_extract_preview_points_empty(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "slicer"))
    import worker as sw
    g = tmp_path / "s.gcode"
    g.write_text("G28\n")
    assert sw.extract_preview_points(g, tmp_path / "p.npz") is False
