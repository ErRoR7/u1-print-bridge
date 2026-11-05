"""Auto color-mapping tests: palette slots -> closest loaded printer filaments.

The mapping reads the printer's live toolhead colors (PAXX print_task_config
via moonraker.toolhead_filaments) and assigns greedily, one toolhead per
slot, good matches only (dE < 35, same material preferred, sufficient
filament preferred). Slots with no good match are left unmapped so the
confirm screen keeps its positional default.
"""
import pytest

from backend import db, moonraker, spoolman, threedmf, workers

HEADS = [
    {"toolhead": "T0", "color_hex": "#FFFFFF", "material": "PLA"},
    {"toolhead": "T1", "color_hex": "#FF8000", "material": "PLA"},
    {"toolhead": "T2", "color_hex": "#004080", "material": "PLA"},
    {"toolhead": "T3", "color_hex": None, "material": None},
]


def _slot(slot, hex6, material="PLA", grams=10.0):
    return {"slot": slot, "required_hex": hex6, "required_material": material,
            "grams": grams}


def test_closest_on_other_toolhead_wins():
    out = workers.auto_map_toolheads([_slot(0, "#FF8000")], HEADS)
    assert out[0]["toolhead"] == 1
    assert out[0]["match"] == "ok"
    assert out[0]["de"] == 0.0


def test_material_beats_closer_color():
    heads = [
        {"toolhead": "T0", "color_hex": "#FF8000", "material": "PETG"},
        {"toolhead": "T1", "color_hex": "#FF6000", "material": "PLA"},
    ]
    out = workers.auto_map_toolheads([_slot(0, "#FF8000", "PLA")], heads)
    assert out[0]["toolhead"] == 1  # PLA orange-red, not PETG orange


def test_one_toolhead_per_slot():
    # Both slots want the same orange on T1; only one may have it.
    slots = [_slot(0, "#FF8000"), _slot(1, "#FF8100")]
    out = workers.auto_map_toolheads(slots, HEADS)
    assert out[0]["toolhead"] == 1  # exact orange
    assert 1 not in out  # second slot stays unmapped, not double-booked
    assert len({v["toolhead"] for v in out.values()}) == 1


def test_far_color_not_mapped():
    out = workers.auto_map_toolheads([_slot(0, "#00FF00")], HEADS)
    assert out == {}


def test_insufficient_filament_deprioritized():
    heads = [
        {"toolhead": "T0", "color_hex": "#FF8000", "material": "PLA"},
        {"toolhead": "T1", "color_hex": "#FF9000", "material": "PLA"},
    ]
    spools = {0: {"remaining_g": 2.0}, 1: {"remaining_g": 500.0}}
    out = workers.auto_map_toolheads([_slot(0, "#FF8000", grams=25.0)], heads, spools)
    assert out[0]["toolhead"] == 1  # T0 is exact but nearly empty


def test_no_heads_or_no_color():
    assert workers.auto_map_toolheads([_slot(0, "#FF8000")], None) == {}
    assert workers.auto_map_toolheads([_slot(0, "#FF8000")], []) == {}
    assert workers.auto_map_toolheads(
        [_slot(0, None)], HEADS) == {}  # as-modeled single color


def test_build_plan_sets_auto_toolhead(tmp_path, monkeypatch):
    conn = db.open_db(str(tmp_path / "t.db"))
    job_id = "job-auto-1"
    db.create_job(conn, job_id, "Coaster", None, "upload")
    spools = [
        {"id": 1, "name": "white", "color_hex": "FFFFFF", "material": "PLA",
         "remaining_g": 500, "location": "T0"},
        {"id": 2, "name": "orange", "color_hex": "FF8000", "material": "PLA",
         "remaining_g": 500, "location": "T1"},
        {"id": 3, "name": "navy", "color_hex": "004080", "material": "PLA",
         "remaining_g": 500, "location": "T2"},
    ]
    monkeypatch.setattr(spoolman, "get_spools", lambda url, **k: spools)
    monkeypatch.setattr(moonraker, "toolhead_filaments", lambda url, **k: HEADS)
    insp = threedmf.Inspection(
        kind="3mf",
        palette=[{"slot": 0, "hex": "FF8000", "material": "PLA"},
                 {"slot": 1, "hex": "004080", "material": "PLA"},
                 {"slot": 2, "hex": "FFFFFF", "material": "PLA"}],
        plates=1,
        volumes={1: 5.0, 2: 2.0, 3: 1.0},
    )
    plan, proposed = workers._build_plan(conn, job_id, insp)
    by_slot = {e["slot"]: e for e in plan}
    # Positional matching already puts the right spools at the right slots;
    # auto-mapping agrees (and would have rescued a shuffled load order).
    assert by_slot[0]["auto_toolhead"] == 1
    assert by_slot[1]["auto_toolhead"] == 2
    assert by_slot[2]["auto_toolhead"] == 0
    assert all(e["auto_match"] == "ok" for e in plan)


def test_build_plan_rescues_shuffled_load(tmp_path, monkeypatch):
    """Orange is loaded on T2 and navy on T0: auto-map follows the colors,
    not the slot positions."""
    conn = db.open_db(str(tmp_path / "t.db"))
    job_id = "job-auto-2"
    db.create_job(conn, job_id, "Coaster", None, "upload")
    spools = [
        {"id": 1, "name": "navy", "color_hex": "004080", "material": "PLA",
         "remaining_g": 500, "location": "T0"},
        {"id": 2, "name": "orange", "color_hex": "FF8000", "material": "PLA",
         "remaining_g": 500, "location": "T2"},
    ]
    shuffled_heads = [
        {"toolhead": "T0", "color_hex": "#004080", "material": "PLA"},
        {"toolhead": "T1", "color_hex": "#FFFFFF", "material": "PLA"},
        {"toolhead": "T2", "color_hex": "#FF8000", "material": "PLA"},
        {"toolhead": "T3", "color_hex": None, "material": None},
    ]
    monkeypatch.setattr(spoolman, "get_spools", lambda url, **k: spools)
    monkeypatch.setattr(moonraker, "toolhead_filaments", lambda url, **k: shuffled_heads)
    insp = threedmf.Inspection(
        kind="3mf",
        palette=[{"slot": 0, "hex": "FF8000", "material": "PLA"},
                 {"slot": 1, "hex": "004080", "material": "PLA"}],
        plates=1,
        volumes={1: 5.0, 2: 2.0},
    )
    plan, _ = workers._build_plan(conn, job_id, insp)
    by_slot = {e["slot"]: e for e in plan}
    assert by_slot[0]["auto_toolhead"] == 2  # orange lives on T2
    assert by_slot[1]["auto_toolhead"] == 0  # navy lives on T0
