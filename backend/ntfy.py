"""ntfy push notifications for the print bridge's own pipeline events.

Printer-lifecycle events (started/complete/failed/paused) are already covered
by OctoEverywhere, so this module only fires for app-side events:
download/slice/upload failures, filament-too-low, and successful completion.

send() is a no-op (logged) when NTFY_TOPIC is empty.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("u1-bridge.ntfy")

# (title template, body template, tags, priority 1..5)
_TEMPLATES = {
    "fetch_failed": (
        "U1 bridge: download failed",
        "Couldn't download '{title}': {error}",
        ["x", "package"],
        4,
    ),
    "inspect_failed": (
        "U1 bridge: model check failed",
        "'{title}' couldn't be inspected: {error}",
        ["x", "mag"],
        4,
    ),
    "slice_failed": (
        "U1 bridge: slicing failed",
        "'{title}' failed to slice: {error}",
        ["x", "printer"],
        4,
    ),
    "upload_failed": (
        "U1 bridge: upload failed",
        "'{title}' couldn't be sent to the printer: {error}",
        ["x", "printer"],
        4,
    ),
    "print_failed": (
        "U1 bridge: print failed",
        "'{title}' failed on the printer: {error}",
        ["x", "fire"],
        5,
    ),
    "failed": (
        "U1 bridge: job failed",
        "'{title}' failed: {error}",
        ["x"],
        4,
    ),
    "filament_low": (
        "U1 bridge: not enough filament",
        "'{title}' needs ~{need}g on {toolhead} but '{spool}' only has {have}g left. "
        "Load a fuller spool and retry.",
        ["warning", "spool"],
        4,
    ),
    "material_mismatch": (
        "U1 bridge: wrong material loaded",
        "'{title}' stopped: {error}",
        ["warning", "spool"],
        4,
    ),
    "done": (
        "U1 bridge: print finished",
        "'{title}' completed in {duration}.",
        ["white_check_mark", "tada"],
        3,
    ),
}


def build_message(event: str, job: dict, **extra) -> tuple[str, str, list[str], int]:
    """Build (title, body, tags, priority) for an event.

    job: dict with at least 'title'; 'error' used when present.
    extra: template-specific fields (e.g. need/have/spool/toolhead, duration).
    """
    if event not in _TEMPLATES:
        raise KeyError(f"unknown ntfy event: {event!r}")
    title_t, body_t, tags, priority = _TEMPLATES[event]
    ctx = {
        "title": job.get("title") or "untitled job",
        "error": job.get("error") or "unknown error",
        **extra,
    }
    try:
        title = title_t.format(**ctx)
    except KeyError:
        title = title_t
    try:
        body = body_t.format(**ctx)
    except KeyError:
        body = body_t
    return title, body, list(tags), priority


def job_url(bridge_url: str, job_id: str) -> str | None:
    if not bridge_url:
        return None
    return bridge_url.rstrip("/") + f"/#job-{job_id}"


def send(
    server: str,
    topic: str,
    title: str,
    body: str,
    tags: list[str] | None = None,
    priority: int = 3,
    click: str | None = None,
    timeout: float = 10.0,
    attach_path: str | None = None,
) -> bool:
    """POST one ntfy message. Returns True on 2xx.

    No-op (returns False) when topic is empty -- logged, not an error.
    When attach_path points at a file, the file is uploaded as the message
    body (ntfy attachment) with the text carried in the Message header.
    """
    if not topic:
        log.info("ntfy disabled (no topic); not sending: %s", title)
        return False
    from pathlib import Path

    headers = {"Title": title, "Priority": str(priority)}
    if tags:
        headers["Tags"] = ",".join(tags)
    if click:
        headers["Click"] = click
    url = server.rstrip("/") + "/" + topic
    try:
        attach = Path(attach_path) if attach_path else None
        if attach is not None and attach.is_file():
            headers["Message"] = body.encode("utf-8")[:4096].decode("utf-8", "replace")
            headers["Filename"] = attach.name
            resp = httpx.put(url, content=attach.read_bytes(),
                             headers=headers, timeout=max(timeout, 30.0))
        else:
            resp = httpx.post(url, content=body.encode("utf-8"),
                              headers=headers, timeout=timeout)
        resp.raise_for_status()
        log.info("ntfy sent: %s", title)
        return True
    except Exception as exc:
        log.warning("ntfy send failed (%s): %s", title, exc)
        return False


def notify(server: str, topic: str, event: str, job: dict, bridge_url: str = "",
           attach_path: str | None = None, **extra) -> bool:
    """Build + send a notification for an event. Returns True if sent."""
    title, body, tags, priority = build_message(event, job, **extra)
    click = job_url(bridge_url, job.get("id", "")) if job.get("id") else None
    return send(server, topic, title, body, tags, priority, click=click,
                attach_path=attach_path)


def health(server: str, topic: str, timeout: float = 4.0) -> dict:
    """ntfy probe: disabled when no topic, else check the server root. Never raises."""
    if not topic:
        return {"ok": True, "latency_ms": None, "detail": "disabled (no NTFY_TOPIC)"}
    try:
        resp = httpx.get(server.rstrip("/"), timeout=timeout)
        resp.raise_for_status()
        return {"ok": True, "latency_ms": None, "detail": f"topic '{topic}' configured"}
    except Exception as exc:
        return {"ok": False, "latency_ms": None, "detail": f"{type(exc).__name__}: {exc}"}
