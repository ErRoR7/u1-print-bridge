from backend.colors import delta_e, nearest_spool, normalize_hex


def test_identical_colors_zero_distance():
    assert delta_e("FF0000", "FF0000") == 0.0
    assert delta_e("#ff0000", "FF0000") == 0.0


def test_red_vs_blue_is_large():
    de = delta_e("FF0000", "0000FF")
    assert de > 100, f"expected large distance, got {de}"


def test_red_vs_dark_red_is_small():
    de = delta_e("FF0000", "CC0000")
    assert de < 35, f"expected small distance, got {de}"


def test_normalize_hex_strips_alpha_and_hash():
    assert normalize_hex("#003776FF") == "003776"
    assert normalize_hex("003776") == "003776"
    assert normalize_hex("#D3C5A3") == "D3C5A3"
    assert normalize_hex(None) is None
    assert normalize_hex("notacolor") is None


def test_nearest_spool_picks_correctly():
    spools = [
        {"id": 1, "name": "blue", "color_hex": "0000FF"},
        {"id": 2, "name": "red", "color_hex": "#FF0000"},
        {"id": 3, "name": "no color", "color_hex": None},
    ]
    best, de = nearest_spool("FE0101", spools)
    assert best["id"] == 2
    assert de == delta_e("FE0101", "FF0000")


def test_nearest_spool_no_candidates():
    best, de = nearest_spool("FF0000", [{"id": 1, "color_hex": None}])
    assert best is None and de is None
