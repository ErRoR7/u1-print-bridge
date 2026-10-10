"""Material match check: spool material vs the model's filament type."""
import pytest

from backend import workers


def _row(plan, slot_spools):
    import json
    return {"plan_json": json.dumps(plan), "slot_spools_json": json.dumps(slot_spools)}


def _plan_entry(slot, required, spool):
    return {"slot": slot, "toolhead": f"T{slot}",
            "required_material": required, "spool": spool}


class _Conn:
    def __init__(self):
        self.warnings = []
    # db.append_log(conn, job_id, level, msg)


def _patch(monkeypatch, spools=None, fail=False):
    logged = []

    def fake_get_spools(url):
        if fail:
            raise workers.spoolman.SpoolmanError("down")
        return spools or []

    monkeypatch.setattr(workers.spoolman, "get_spools", fake_get_spools)
    monkeypatch.setattr(workers.db, "append_log",
                        lambda conn, job_id, level, msg: logged.append((level, msg)))
    return logged


def test_materials_compatible_exact_and_variants():
    assert workers._materials_compatible("PLA", "PLA")
    assert workers._materials_compatible("PLA", "PLA+")
    assert workers._materials_compatible("PLA", "Silk PLA")
    assert workers._materials_compatible("PETG", "PETG-CF")
    assert not workers._materials_compatible("PLA", "PETG")
    assert not workers._materials_compatible("ABS", "ASA")


def test_materials_compatible_unknown_never_blocks():
    assert workers._materials_compatible(None, "PLA")
    assert workers._materials_compatible("PLA", None)
    assert workers._materials_compatible("", "")


def test_material_check_pass(monkeypatch):
    spools = [{"id": 7, "name": "Red PLA+", "material": "PLA+", "remaining_g": 500}]
    _patch(monkeypatch, spools)
    row = _row([_plan_entry(0, "PLA", spools[0])], {"0": 7})
    assert workers._material_check(object(), "j1", row) is None


def test_material_check_mismatch(monkeypatch):
    spools = [{"id": 7, "name": "Black PETG", "material": "PETG", "remaining_g": 500}]
    _patch(monkeypatch, spools)
    row = _row([_plan_entry(1, "PLA", spools[0])], {"1": 7})
    err = workers._material_check(object(), "j1", row)
    assert err is not None
    assert "PETG" in err and "PLA" in err and "T1" in err


def test_material_check_no_required_material_skips(monkeypatch):
    # e.g. plain STL with no filament type declared -- nothing to check.
    spools = [{"id": 7, "name": "Black PETG", "material": "PETG", "remaining_g": 500}]
    _patch(monkeypatch, spools)
    row = _row([_plan_entry(0, None, spools[0])], {"0": 7})
    assert workers._material_check(object(), "j1", row) is None


def test_material_check_spoolman_down_warns(monkeypatch):
    logged = _patch(monkeypatch, fail=True)
    row = _row([_plan_entry(0, "PLA", None)], {"0": 7})
    assert workers._material_check(object(), "j1", row) is None
    assert any(lv == "warning" for lv, _ in logged)
