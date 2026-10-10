"""threedmf tests against the 3 REAL samples in /tmp/u1-3mf-samples plus
synthetic 3MF round-trips.

Ground truth (verified 2026-10-09 with unzip):
- watchful-owl.3mf (Bambu): filament_colour ['#D3C5A3','#7C4B00','#FFFFFF','#000000'],
  all PETG, extruder metadata {'1'}, Metadata/plate_1.{json,png} present.
- parrot U1 project: filament_colour ['#003776FF','#E72F1DFF','#FBFFDB','#F8F81CFF'],
  all PLA, extruders {'3'}, plates 1+2 (png only, no plate_N.json).
- sea slug U1 project: extruder indices {'1','7'} -> virtual extruder 7 > 4,
  so inspect() must flag it unsupported.
"""
import json
import os
import zipfile
from pathlib import Path

import pytest

from backend.threedmf import InspectorError, inspect

SAMPLES = Path(os.environ.get("U1_3MF_SAMPLES", "/tmp/u1-3mf-samples"))

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Minimal 1x1 PNG (opaque red) for synthetic thumbnails.
MINI_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415478da626001000000ffff03000006000557bfabd40000000049454e44ae426082"
)


def test_watchful_owl_palette_order_and_materials():
    insp = inspect(SAMPLES / "watchful-owl.3mf")
    assert insp.kind == "3mf"
    assert [p["hex"] for p in insp.palette] == ["D3C5A3", "7C4B00", "FFFFFF", "000000"]
    assert [p["material"] for p in insp.palette] == ["PETG"] * 4
    assert [p["slot"] for p in insp.palette] == [0, 1, 2, 3]
    assert insp.extruders == [1]
    assert insp.plates >= 1
    assert insp.thumbnail_bytes is not None
    assert insp.thumbnail_bytes[:8] == PNG_MAGIC


def test_parrot_u1_palette_alpha_stripped_and_two_plates():
    insp = inspect(SAMPLES / "展翅鹦鹉【项目】U1.3mf")
    assert [p["hex"] for p in insp.palette] == ["003776", "E72F1D", "FBFFDB", "F8F81C"]
    assert [p["material"] for p in insp.palette] == ["PLA"] * 4
    assert insp.extruders == [3]
    # No Metadata/plate_N.json in this file -> plate count falls back to pngs.
    assert insp.plates == 2
    assert insp.thumbnail_bytes is not None
    assert insp.thumbnail_bytes[:8] == PNG_MAGIC


def test_sea_slug_virtual_extruder_unsupported():
    with pytest.raises(InspectorError) as excinfo:
        inspect(SAMPLES / "海蛞蝓【项目】U1  sea.3mf")
    assert excinfo.value.code == "unsupported"
    assert "virtual" in str(excinfo.value).lower()


def _make_3mf(path, *, project_settings=None, extruders=(1,), plates=(1,)):
    model_settings = '<?xml version="1.0" encoding="UTF-8"?>\n<modelsettings>\n'
    for i, e in enumerate(extruders):
        model_settings += (
            '  <object id="%d"><part id="%d">'
            '<metadata key="extruder" value="%d"/></part></object>\n' % (i, i, e)
        )
    model_settings += "</modelsettings>\n"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("3D/3dmodel.model", "<model/>")
        if project_settings is not None:
            zf.writestr("Metadata/project_settings.config", json.dumps(project_settings))
        zf.writestr("Metadata/model_settings.config", model_settings)
        for p in plates:
            zf.writestr("Metadata/plate_%d.json" % p, json.dumps({"version": 2}))
            zf.writestr("Metadata/plate_%d.png" % p, MINI_PNG)
    return path


def test_synthetic_3mf_round_trip(tmp_path):
    p = _make_3mf(
        tmp_path / "synth.3mf",
        project_settings={
            "filament_colour": ["#FF0000FF", "#00FF00"],
            "filament_type": ["PLA", "PETG"],
        },
        extruders=(1, 2),
    )
    insp = inspect(p)
    assert insp.kind == "3mf"
    assert [s["hex"] for s in insp.palette] == ["FF0000", "00FF00"]
    assert [s["material"] for s in insp.palette] == ["PLA", "PETG"]
    assert insp.extruders == [1, 2]
    assert insp.plates == 1
    assert insp.thumbnail_bytes[:8] == PNG_MAGIC


def test_synthetic_bare_3mf_single_slot(tmp_path):
    p = _make_3mf(tmp_path / "bare.3mf", project_settings=None)
    insp = inspect(p)
    assert insp.palette == [{"slot": 0, "hex": None, "material": None}]


def test_synthetic_virtual_extruder_rejected(tmp_path):
    p = _make_3mf(
        tmp_path / "blend.3mf",
        project_settings={"filament_colour": ["#FF0000FF"] * 4, "filament_type": ["PLA"] * 4},
        extruders=(1, 7),
    )
    with pytest.raises(InspectorError) as excinfo:
        inspect(p)
    assert excinfo.value.code == "unsupported"


def test_not_a_3mf(tmp_path):
    p = tmp_path / "nope.3mf"
    p.write_bytes(b"this is not a zip")
    with pytest.raises(InspectorError):
        inspect(p)


def test_stl_single_slot(tmp_path):
    p = tmp_path / "cube.stl"
    p.write_bytes(b"solid cube\nendsolid cube\n")
    insp = inspect(p)
    assert insp.kind == "stl"
    assert insp.palette == [{"slot": 0, "hex": None, "material": None}]
    assert insp.plates == 1


def _make_3mf_with_box(path, wx=10.0, wy=20.0, wz=30.0, project_settings=None):
    verts = "".join(
        '<vertex x="%s" y="%s" z="%s"/>\n' % (x, y, z)
        for x in (0, wx) for y in (0, wy) for z in (0, wz))
    obj = ('<object id="2" type="model"><mesh><vertices>%s</vertices>'
           '<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>' % verts)
    model = ('<?xml version="1.0"?><model unit="millimeter"><resources>%s</resources>'
             '<build><item objectid="2"/></build></model>' % obj)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("3D/3dmodel.model", model)
        if project_settings is not None:
            zf.writestr("Metadata/project_settings.config", json.dumps(project_settings))
    return path


def test_3mf_dimensions_measured(tmp_path):
    p = _make_3mf_with_box(
        tmp_path / "box.3mf", 10, 20, 30,
        project_settings={"filament_colour": ["#FF0000"], "filament_type": ["PLA"]})
    insp = inspect(p)
    assert insp.dimensions == {"x": 10.0, "y": 20.0, "z": 30.0}


def test_3mf_dimensions_none_without_vertices(tmp_path):
    p = _make_3mf(tmp_path / "bare.3mf", project_settings=None)
    assert inspect(p).dimensions is None


def _make_binary_stl(path, tris):
    import struct
    with open(path, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", len(tris)))
        for tri in tris:
            f.write(struct.pack("<12fH", 0, 0, 0, *tri, 0))


def test_stl_dimensions_binary(tmp_path):
    p = tmp_path / "tri.stl"
    _make_binary_stl(p, [(0, 0, 0, 10, 0, 0, 0, 20, 5)])
    assert inspect(p).dimensions == {"x": 10.0, "y": 20.0, "z": 5.0}


def test_stl_dimensions_ascii(tmp_path):
    p = tmp_path / "a.stl"
    p.write_bytes(b"solid a\nfacet normal 0 0 0\nouter loop\n"
                  b"vertex 0 0 0\nvertex 4 0 0\nvertex 0 6 2\n"
                  b"endloop\nendfacet\nendsolid a\n")
    assert inspect(p).dimensions == {"x": 4.0, "y": 6.0, "z": 2.0}


def test_estimate_volumes_per_extruder(tmp_path):
    import io, zipfile
    from backend.threedmf import estimate_volumes

    def cube(obj_id, size, ox):
        v = [(x + ox, y, z) for z in (0, size) for y in (0, size) for x in (0, size)]
        tris = [(0,1,3),(0,3,2),(4,6,7),(4,7,5),(0,4,5),(0,5,1),
                (2,3,7),(2,7,6),(0,2,6),(0,6,4),(1,5,7),(1,7,3)]
        verts = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in v)
        tr = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
        return (f'<object id="{obj_id}" type="model"><mesh><vertices>{verts}</vertices>'
                f'<triangles>{tr}</triangles></mesh></object>')

    model_xml = ('<model><resources>' + cube(2, 10, 0) + cube(3, 20, 50) +
                 '</resources><build><item objectid="2"/><item objectid="3"/></build></model>')
    settings = ('<config><object id="2"><metadata key="extruder" value="1"/></object>'
                '<object id="3"><metadata key="extruder" value="2"/></object></config>')
    p3 = tmp_path / "v.3mf"
    with zipfile.ZipFile(p3, "w") as zf:
        zf.writestr("3D/3dmodel.model", model_xml)
        zf.writestr("Metadata/model_settings.config", settings)
    vols = estimate_volumes(p3)
    assert abs(vols[1] - 1.0) < 0.01
    assert abs(vols[2] - 8.0) < 0.01
