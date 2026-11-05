"""Bambu/MakerWorld 3MF -> Snapmaker U1 conversion via the u1convert sidecar.

The bridge's slicer strips foreign project settings and slices with its own
U1 profiles, so conversion here is about geometry + paint + extruder mapping:
a Bambu project file becomes a U1 project file the inspector and the headless
Orca CLI can both handle. Files with more than 4 filament colours are NOT
converted -- there is no automatic answer for painted >4-colour work, so they
keep the bridge's existing behaviour (the inspector rejects extruder indices
above 4 with a clear message).

The converter runs as a separate container from the prebuilt image
``ghcr.io/thadius83/bambu-to-snapmaker-u1`` (PolyForm Noncommercial licence --
personal use only, NOT vendored into this repo). When CONVERTER_URL is empty
or the sidecar is unreachable, conversion is skipped and the original file is
used unchanged: conversion never fails a job.
"""
from __future__ import annotations

import json
import logging
import time
import zipfile
from pathlib import Path

import httpx

log = logging.getLogger("u1-bridge.converter")

# Reference profile the sidecar converts against. The bridge discards embedded
# settings at slice time (it loads its own machine/process/filament JSONs), so
# the profile choice only affects the converter's identity swap, not the print.
DEFAULT_REFERENCE_PROFILE = "0.20 Standard"

# U1 has 4 physical toolheads; only convert when the source fits them.
MAX_CONVERT_COLOURS = 4

CONVERT_TIMEOUT_S = 120.0
HEALTH_TIMEOUT_S = 5.0

PROJECT_SETTINGS = "Metadata/project_settings.config"

# httpx must not use env proxies for LAN hosts (same as moonraker/spoolman).
_NO_PROXY = {"trust_env": False}


class ConverterError(Exception):
    """Sidecar conversion failed (HTTP error, timeout, bad response)."""


def _read_project_settings(path: str | Path) -> dict | None:
    """Parsed Metadata/project_settings.config, or None when absent/unreadable."""
    try:
        with zipfile.ZipFile(path) as zf:
            if PROJECT_SETTINGS not in zf.namelist():
                return None
            return json.loads(zf.read(PROJECT_SETTINGS))
    except Exception:
        return None


def printer_model(path: str | Path) -> str | None:
    """The printer_model declared in the 3MF project settings, if any."""
    settings = _read_project_settings(path)
    if not settings:
        return None
    model = settings.get("printer_model")
    return model if isinstance(model, str) and model.strip() else None


def is_bambu_3mf(path: str | Path) -> bool:
    """True when the file declares itself a Bambu Lab project."""
    model = printer_model(path)
    return bool(model) and model.strip().lower().startswith("bambu lab")


def colour_count(path: str | Path) -> int | None:
    """Number of filament colours in the project settings, if declared."""
    settings = _read_project_settings(path)
    if not settings:
        return None
    colours = settings.get("filament_colour")
    return len(colours) if isinstance(colours, list) else None


def should_convert(path: str | Path) -> tuple[bool, str]:
    """Decide whether the file is a convertible Bambu project.

    Returns (True, reason) for Bambu projects with at most 4 filament
    colours, else (False, reason). Anything already U1, bare, or non-Bambu
    is left alone.
    """
    path = Path(path)
    if not path.is_file():
        return False, "file not found"
    if path.suffix.lower() != ".3mf":
        return False, "not a 3MF"
    model = printer_model(path)
    if not model:
        return False, "no printer identity in project settings"
    if not model.strip().lower().startswith("bambu lab"):
        return False, f"not a Bambu project ({model})"
    n = colour_count(path)
    if n is not None and n > MAX_CONVERT_COLOURS:
        return False, f"{n} filament colours exceeds the U1's 4 toolheads"
    return True, f"Bambu project ({model})"


def convert_file(base_url: str, src_path: str | Path, dest_path: str | Path,
                 timeout: float = CONVERT_TIMEOUT_S) -> dict:
    """Convert a 3MF via the sidecar and save the result to dest_path.

    POSTs the file to /api/convert, then downloads the converted 3MF from
    /api/download/{job_id} immediately (sidecar files expire after 5 min).
    Returns the converter's diff counts dict (possibly empty). Raises
    ConverterError on any HTTP error, timeout, or malformed response.
    """
    src_path, dest_path = Path(src_path), Path(dest_path)
    url = (base_url or "").rstrip("/")
    if not url:
        raise ConverterError("no converter URL configured")
    try:
        with httpx.Client(timeout=timeout, **_NO_PROXY) as client:
            with src_path.open("rb") as fh:
                resp = client.post(
                    f"{url}/api/convert",
                    files={"file": (src_path.name, fh, "application/octet-stream")},
                    data={
                        "reference_profile": DEFAULT_REFERENCE_PROFILE,
                        "apply_rules": "true",
                        "clamp_speeds": "true",
                        "preserve_color_painting": "true",
                        "insert_swap_pauses": "false",
                    },
                )
            resp.raise_for_status()
            body = resp.json()
            job_id = body.get("job_id") if isinstance(body, dict) else None
            if not job_id:
                raise ConverterError("converter response had no job_id")
            with client.stream("GET", f"{url}/api/download/{job_id}") as dl:
                dl.raise_for_status()
                with dest_path.open("wb") as out:
                    for chunk in dl.iter_bytes():
                        out.write(chunk)
            diff = body.get("diff") if isinstance(body, dict) else None
            counts = diff.get("counts") if isinstance(diff, dict) else None
            return counts if isinstance(counts, dict) else {}
    except ConverterError:
        raise
    except Exception as exc:
        raise ConverterError(f"{type(exc).__name__}: {exc}") from exc


def health(base_url: str, timeout: float = HEALTH_TIMEOUT_S) -> dict:
    """Cheap reachability probe for /api/health. Never raises."""
    if not (base_url or "").strip():
        return {"ok": True, "latency_ms": None, "detail": "not configured"}
    started = time.monotonic()
    try:
        resp = httpx.get(base_url.rstrip("/") + "/api/health",
                         timeout=timeout, **_NO_PROXY)
        resp.raise_for_status()
        return {"ok": True,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "detail": "reachable"}
    except Exception as exc:
        return {"ok": False, "latency_ms": None,
                "detail": f"{type(exc).__name__}: {exc}"}
