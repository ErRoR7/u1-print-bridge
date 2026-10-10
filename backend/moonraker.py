"""Moonraker client for the Snapmaker U1 (PAXX firmware retains Moonraker).

Endpoints used (per Moonraker docs):
- POST /server/files/upload  (multipart: file + root=gcodes, path=..., print=true)
- GET  /printer/objects/query?print_stats&virtual_sdcard&display_status
- POST /printer/print/pause | /printer/print/resume | /printer/print/cancel
- GET  /server/info  (reachability probe)

Auth: stock U1 Moonraker is typically open on LAN. If PAXX ever requires an
API key, set it via MOONRAKER_API_KEY and it is sent as X-Api-Key.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

_API_KEY = os.environ.get("MOONRAKER_API_KEY", "")

# Moonraker lives on the LAN (RFC1918). Never route it through an egress
# proxy: trust_env=False keeps proxy env vars (which may be broken or
# inappropriate for LAN hosts) out of these calls.
_NO_PROXY = {"trust_env": False}


class MoonrakerError(Exception):
    """Moonraker is unreachable or rejected the request."""


def _headers() -> dict:
    return {"X-Api-Key": _API_KEY} if _API_KEY else {}


def _url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def health(base_url: str, timeout: float = 4.0) -> dict:
    """Probe Moonraker reachability. Returns {ok, latency_ms, detail}; never raises."""
    started = time.monotonic()
    try:
        resp = httpx.get(_url(base_url, "/server/info"), headers=_headers(),
                         timeout=timeout, **_NO_PROXY)
        resp.raise_for_status()
        info = resp.json().get("result", {})
        latency_ms = int((time.monotonic() - started) * 1000)
        version = info.get("moonraker_version", "?")
        klippy = info.get("klippy_state", "?")
        return {"ok": True, "latency_ms": latency_ms, "detail": f"moonraker {version}, klippy {klippy}"}
    except Exception as exc:
        return {"ok": False, "latency_ms": None, "detail": f"{type(exc).__name__}: {exc}"}


def preflight_state(base_url: str, timeout: float = 8.0) -> str:
    """Return print_stats.state ('printing', 'paused', 'standby', ...).

    Raises MoonrakerError when the printer cannot be reached.
    """
    try:
        resp = httpx.get(
            _url(base_url, "/printer/objects/query"),
            params={"print_stats": "", "virtual_sdcard": "", "display_status": ""},
            headers=_headers(),
            timeout=timeout,
            **_NO_PROXY,
        )
        resp.raise_for_status()
        result = resp.json().get("result", {}).get("status", {})
        return result.get("print_stats", {}).get("state", "unknown")
    except Exception as exc:
        raise MoonrakerError(f"Printer unreachable at {base_url} ({exc})") from exc


def query(base_url: str, timeout: float = 8.0) -> dict:
    """Full status query: print_stats, virtual_sdcard, display_status."""
    try:
        resp = httpx.get(
            _url(base_url, "/printer/objects/query"),
            params={"print_stats": "", "virtual_sdcard": "", "display_status": ""},
            headers=_headers(),
            timeout=timeout,
            **_NO_PROXY,
        )
        resp.raise_for_status()
        return resp.json().get("result", {}).get("status", {})
    except Exception as exc:
        raise MoonrakerError(f"Printer query failed ({exc})") from exc


def webcams(base_url: str, timeout: float = 8.0) -> list[dict]:
    """Moonraker's configured webcam list (PAXX exposes the U1 camera here).

    Returns the raw webcam dicts; raises MoonrakerError when unreachable.
    """
    try:
        resp = httpx.get(_url(base_url, "/server/webcams/list"),
                         headers=_headers(), timeout=timeout, **_NO_PROXY)
        resp.raise_for_status()
        return resp.json().get("result", {}).get("webcams", []) or []
    except Exception as exc:
        raise MoonrakerError(f"Webcam list query failed ({exc})") from exc


def camera_urls(base_url: str, timeout: float = 8.0) -> dict | None:
    """Absolute camera URLs for the first enabled Moonraker webcam.

    PAXX extended firmware serves the U1 camera on the printer's web server
    (port 80): /webcam/snapshot.jpg, /webcam/stream.mjpg, /webcam/webrtc.
    Returns {name, stream_url (MJPEG), snapshot_url, webrtc_url} or None when
    no enabled webcam is reported. Raises MoonrakerError when unreachable.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(base_url)
    origin = f"{parts.scheme}://{parts.hostname}"  # camera is on :80, not Moonraker's port
    cams = webcams(base_url, timeout=timeout)
    cam = next((w for w in cams if w.get("enabled", True)), None)
    if cam is None:
        return None
    stream_path = str(cam.get("stream_url") or "")
    snap_path = str(cam.get("snapshot_url") or "")
    # stream_url may be a webrtc endpoint; the same PAXX camera service also
    # serves plain MJPEG at <prefix>/stream.mjpg, which an <img> tag can show.
    prefix = stream_path.rsplit("/", 1)[0] if "/" in stream_path else "/webcam"
    return {
        "name": cam.get("name") or "printer",
        "stream_url": origin + prefix + "/stream.mjpg",
        "snapshot_url": origin + snap_path if snap_path.startswith("/") else snap_path,
        "webrtc_url": origin + stream_path if stream_path.startswith("/") else stream_path,
    }


def toolhead_filaments(base_url: str, timeout: float = 8.0) -> list[dict] | None:
    """Per-toolhead filament info straight from the printer (PAXX firmware).

    Queries the ``print_task_config`` Klipper object, which carries
    ``filament_color_rgba`` (list of RRGGBBAA per extruder, index == toolhead)
    -- the same data Fluidd's AFC panel shows.

    Returns [{toolhead: 'T0', color_hex: '#rrggbb'|None, material: str|None}, ...]
    (always 4 entries on the U1), or None when the object is unavailable
    (printer unreachable, or firmware without the object). Never raises.
    """
    try:
        resp = httpx.get(
            _url(base_url, "/printer/objects/query"),
            params={"print_task_config": ""},
            headers=_headers(),
            timeout=timeout,
            **_NO_PROXY,
        )
        resp.raise_for_status()
        ptc = resp.json().get("result", {}).get("status", {}).get("print_task_config") or {}
    except Exception:
        return None
    if not isinstance(ptc, dict):
        return None

    rgba = ptc.get("filament_color_rgba") or []
    multi = ptc.get("filament_color_multi") or []

    # Material may live under several keys depending on firmware version;
    # take the first one that yields a per-extruder list.
    materials: list = []
    for key in ("filament_material", "filament_type", "material", "filament_materials"):
        val = ptc.get(key)
        if isinstance(val, (list, tuple)) and val:
            materials = list(val)
            break

    out = []
    for n in range(4):
        color = None
        if n < len(rgba) and isinstance(rgba[n], str):
            v = rgba[n].strip().lstrip("#")
            if len(v) == 8:
                v = v[:6]
            if len(v) == 6 and all(c in "0123456789abcdefABCDEF" for c in v):
                color = "#" + v.lower()
        if color is None and n < len(multi) and isinstance(multi[n], dict):
            cols = multi[n].get("colors") or []
            if cols and isinstance(cols[0], str):
                v = cols[0].strip().lstrip("#")
                if len(v) >= 6 and all(c in "0123456789abcdefABCDEF" for c in v[:6]):
                    color = "#" + v[:6].lower()
        material = None
        if n < len(materials) and materials[n]:
            material = str(materials[n])
        out.append({"toolhead": f"T{n}", "color_hex": color, "material": material})
    if all(e["color_hex"] is None and e["material"] is None for e in out):
        return None
    return out


def upload(
    base_url: str,
    gcode_path: str | Path,
    dest_filename: str,
    print: bool = True,
    timeout: float = 120.0,
) -> dict:
    """Upload gcode to Moonraker. print=True starts the print immediately.

    Returns the parsed JSON response.
    """
    gcode_path = Path(gcode_path)
    try:
        with gcode_path.open("rb") as fh:
            files = {"file": (dest_filename, fh, "application/octet-stream")}
            data = {"root": "gcodes", "path": "printbridge/", "print": "true" if print else "false"}
            resp = httpx.post(
                _url(base_url, "/server/files/upload"),
                files=files,
                data=data,
                headers=_headers(),
                timeout=timeout,
                **_NO_PROXY,
            )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        raise MoonrakerError(f"Upload failed ({exc})") from exc


def _post(base_url: str, path: str, timeout: float = 15.0) -> None:
    try:
        resp = httpx.post(_url(base_url, path), headers=_headers(), timeout=timeout, **_NO_PROXY)
        resp.raise_for_status()
    except Exception as exc:
        raise MoonrakerError(f"POST {path} failed ({exc})") from exc


def pause(base_url: str) -> None:
    _post(base_url, "/printer/print/pause")


def resume(base_url: str) -> None:
    _post(base_url, "/printer/print/resume")


def cancel(base_url: str) -> None:
    _post(base_url, "/printer/print/cancel")
