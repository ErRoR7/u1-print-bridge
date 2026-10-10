# U1 Print Bridge — backend

FastAPI service owning the job pipeline:

```
created -> fetching -> inspecting -> awaiting_confirm -> slicing
    -> uploading -> printing -> done        (+ failed / cancelled)
```

The slicer step is performed by the separate `slicer/` service, which claims
jobs in `slicing` status from the same SQLite DB (shared via the `/data`
volume) and hands them back as `uploading`.

## Positional slot mapping (read this first)

Bambu/Orca/U1 brush-paint data is a proprietary nibble-packed binary tree.
Rather than decode/rewrite it, the bridge maps 3MF filament slots
**positionally** to toolheads: palette slot 0 → T0, slot 1 → T1, … The 3MF is
passed to the slicer **unmodified** (`threedmf.py` is parse-only). The confirm
screen guides physical spool placement ("load red PLA into T1"), verified live
against Spoolman. Models using virtual/mixed extruders (extruder index > 4 in
`Metadata/model_settings.config`, e.g. U1 Full Spectrum blends) are rejected
at inspect time with a clear error.

## Spoolman is READ-ONLY

The bridge reads spool color/material/remaining grams/location from Spoolman
to build the color plan and to sanity-check filament sufficiency. It **never
writes** to Spoolman: the printer deducts filament usage itself via its own
tracking. There is intentionally no deduct/write path anywhere in this code.

## Layout

- `app.py` — FastAPI routes, static frontend serving, startup (restart
  recovery + health probes), background threads
- `db.py` — sqlite3 (WAL), schema, state-machine helpers, atomic claim
- `workers.py` — pipeline thread, upload+monitor thread, restart recovery
- `threedmf.py` — parse-only 3MF/STL inspection (palette, extruders, plates,
  thumbnail); raises `InspectorError` with user-friendly messages
- `fetchers.py` — Printables GraphQL download, MakerWorld best-effort
  (any failure → manual-upload fallback; login walls are never bypassed),
  direct-URL download with sanity checks
- `spoolman.py` — read-only spool list client
- `moonraker.py` — upload (print=true), status query, pause/resume/cancel,
  preflight
- `ntfy.py` — pipeline-event notifications (no-op when `NTFY_TOPIC` is empty)
- `colors.py` — hex utils + CIE76 delta-E + nearest-spool matching

## Env vars

See `../.env.example`. All are read at startup: `PRINTER_URL`,
`SPOOLMAN_URL`, `NTFY_TOPIC`, `NTFY_SERVER`, `MAKERWORLD_SESSION_COOKIE`,
`DATA_DIR` (default `/data`), `BRIDGE_URL`, `FRONTEND_DIR`.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
DATA_DIR=/tmp/pb-data .venv/bin/uvicorn backend.app:app --port 8080
# from the repo root so backend/ is importable and ../frontend resolves
```

## Tests

```bash
.venv/bin/python -m pytest backend/tests -q
```

`test_threedmf.py` parses the three real sample 3MFs (ground truth asserted
from the actual files) plus synthetic 3MF round-trips.
