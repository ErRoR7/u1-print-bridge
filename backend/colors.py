"""Color utilities for the U1 print bridge.

CIE76 delta-E is implemented properly: sRGB -> linear RGB -> XYZ (D65) ->
CIELAB, then Euclidean distance in Lab space. Used to match a 3MF palette
slot against the spools Spoolman reports as loaded.
"""
from __future__ import annotations

import math
import re

_HEX_RE = re.compile(r"^[0-9a-fA-F]{6}$")


def normalize_hex(value: str | None) -> str | None:
    """Normalize '#RRGGBB[AA]' (or 'RRGGBB[AA]') to uppercase 'RRGGBB'.

    Orca/Bambu 3MFs store filament_colour with an alpha suffix ('#RRGGBBAA');
    some files omit it. Returns None for anything that is not hex.
    """
    if not value:
        return None
    v = value.strip().lstrip("#")
    if len(v) == 8:  # RRGGBBAA -> strip alpha
        v = v[:6]
    if len(v) != 6 or not _HEX_RE.match(v):
        return None
    return v.upper()


def hex_to_rgb(hex6: str) -> tuple[float, float, float]:
    h = normalize_hex(hex6)
    if h is None:
        raise ValueError(f"not a hex color: {hex6!r}")
    return tuple(int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_xyz(r: float, g: float, b: float) -> tuple[float, float, float]:
    """Linear sRGB (D65) -> CIE XYZ."""
    r, g, b = _srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b)
    x = r * 0.4124564 + g * 0.3575761 + b * 0.1804375
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = r * 0.0193339 + g * 0.1191920 + b * 0.9503041
    return x, y, z


def xyz_to_lab(x: float, y: float, z: float) -> tuple[float, float, float]:
    """XYZ -> CIELAB with the D65 reference white."""
    xn, yn, zn = 0.95047, 1.00000, 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else (7.787 * t + 16 / 116)

    fx, fy, fz = f(x / xn), f(y / yn), f(z / zn)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def hex_to_lab(hex6: str) -> tuple[float, float, float]:
    return xyz_to_lab(*rgb_to_xyz(*hex_to_rgb(hex6)))


def delta_e(hex_a: str, hex_b: str) -> float:
    """CIE76 delta-E between two hex colors. 0.0 for identical colors."""
    la, aa, ba = hex_to_lab(hex_a)
    lb, ab, bb = hex_to_lab(hex_b)
    return math.sqrt((la - lb) ** 2 + (aa - ab) ** 2 + (ba - bb) ** 2)


def nearest_spool(required_hex: str, spools: list[dict]) -> tuple[dict | None, float | None]:
    """Return (best_spool, delta_e) for required_hex, or (None, None) if no
    candidate spool has a usable color."""
    best = None
    best_de = None
    for spool in spools:
        color = normalize_hex(spool.get("color_hex"))
        if color is None:
            continue
        de = delta_e(required_hex, color)
        if best_de is None or de < best_de:
            best, best_de = spool, de
    return best, best_de
