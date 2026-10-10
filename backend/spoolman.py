"""Spoolman client for the U1 print bridge -- READ-ONLY.

The bridge uses Spoolman purely as a source of truth for what is loaded where
(spool color, material, remaining grams, location). It never writes to
Spoolman: the printer itself deducts filament usage via its own tracking, so
there is intentionally NO deduct()/use/measure/write path in this module.

GET {SPOOLMAN_URL}/api/v1/spool -> normalized spool list:
    {id, name, color_hex, material, remaining_g, location}
"""
from __future__ import annotations

import httpx

from .colors import normalize_hex


class SpoolmanError(Exception):
    """Spoolman is unreachable or returned something unusable."""


# Spoolman lives on the LAN (RFC1918). Never route it through an egress
# proxy: trust_env=False keeps proxy env vars (which may be broken or
# inappropriate for LAN hosts) out of these calls.
_NO_PROXY = {"trust_env": False}


def get_spools(base_url: str, timeout: float = 8.0) -> list[dict]:
    """Fetch and normalize the spool list. Raises SpoolmanError on failure."""
    url = base_url.rstrip("/") + "/api/v1/spool"
    try:
        # Ask for a big page: the default page size would silently drop spools
        # from large libraries, which breaks toolhead matching downstream.
        resp = httpx.get(url, params={"limit": 1000}, timeout=timeout, **_NO_PROXY)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise SpoolmanError(f"Spoolman request failed ({exc}). Is Spoolman running at {base_url}?") from exc
    if not isinstance(data, list):
        raise SpoolmanError(f"Unexpected Spoolman response from {url}.")
    return [normalize_spool(s) for s in data if isinstance(s, dict)]


def normalize_spool(spool: dict) -> dict:
    """Normalize one raw Spoolman spool object."""
    filament = spool.get("filament") or {}
    return {
        "id": spool.get("id"),
        "name": filament.get("name") or spool.get("comment") or f"Spool {spool.get('id')}",
        "color_hex": normalize_hex(filament.get("color_hex")),
        "material": (filament.get("material") or None),
        "remaining_g": spool.get("remaining_weight"),
        "location": spool.get("location"),
    }


def spools_at_toolhead(spools: list[dict], toolhead: int) -> list[dict]:
    """Spools whose Spoolman location names toolhead T0..T3.

    Location matching is textual: 'T1', 'printer T1', 'Toolhead 2' etc.
    toolhead is 0-based (0 -> T0).
    """
    want = f"T{toolhead}"
    out = []
    for spool in spools:
        loc = (spool.get("location") or "").upper().replace(" ", "")
        if want in loc or f"TOOLHEAD{toolhead}" in loc or f"EXTRUDER{toolhead + 1}" in loc:
            out.append(spool)
    return out


def health(base_url: str, timeout: float = 4.0) -> dict:
    """Cheap reachability probe for /api/health. Never raises."""
    url = base_url.rstrip("/") + "/api/v1/spool"
    try:
        resp = httpx.get(url, params={"limit": 1}, timeout=timeout, **_NO_PROXY)
        resp.raise_for_status()
        count = resp.headers.get("X-Total-Count")
        detail = f"{count} spools" if count is not None else "reachable"
        return {"ok": True, "latency_ms": None, "detail": detail}
    except Exception as exc:
        return {"ok": False, "latency_ms": None, "detail": f"{type(exc).__name__}: {exc}"}
