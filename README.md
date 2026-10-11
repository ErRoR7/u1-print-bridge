# U1 Print Bridge

One button on a phone → multi-color print on the Snapmaker U1. Paste a model
link (or upload a 3MF), check the color→slot mapping,
tap Start print. No slicer knowledge needed.

This repo holds the **frontend** (vanilla HTML/CSS/JS, no build step) plus the
**deployment files** (`docker-compose.yml`, `.env.example`). The backend
(FastAPI, in `backend/`) and the slicer worker image (in `slicer/`) are built
alongside — this compose file expects both directories to exist.

## How a print flows (the 5-step story)

1. **Find a model.** Paste a Printables or MakerWorld link,
   or upload a 3MF file you saved yourself. (The old in-app Printables
   search was removed — paste the link from your browser instead.)
2. **The bridge fetches and inspects it.** It downloads the model, reads the
   color plan from the 3MF, and checks what's loaded in the printer slots.
   (Time and filament estimates are calculated during slicing, after you
   confirm.)
3. **You check the colors.** The app shows each color the model needs next to
   the spool that's actually sitting in that printer slot (read live from
   Spoolman): "Slot 1 needs red PLA → printer slot 1 (T0) has red PLA, 245 g
   left ✓". If a slot doesn't match, you can pick a different spool from a
   list — or physically load the right spool and update Spoolman. You can also
   set quality, scale/copies, and force supports on or off here. A preview
   panel at the bottom renders an isometric preview of the actual sliced
   toolpath in the colors you mapped, so you can see exactly how the print
   will look before starting. Each model color is also listed next to the
   toolhead you picked, so you can double-check the mapping.
4. **Tap Start print.** The bridge slices the model with the Snapmaker U1
   profiles, then verifies each toolhead has *enough* filament *of the right
   material* before anything goes to the printer.
5. **Watch it go.** The print card shows live progress with pause / resume /
   cancel buttons, plus a remaining-time estimate ("Printing — 62% · ~38 min
   left", from the slicer's time estimate and live progress). The sliced
   G-code gets exact 96x96 and 300x300 preview thumbnails spliced in (the
   sizes the U1 touchscreen looks for) so the model picture shows on the
   printer screen, not just in Fluidd. When the print finishes, the bridge
   snaps a photo while the bed is still up (around 99%) so the finished
   model is actually in frame.
6. **Reprint.** Finished, failed, or cancelled jobs get a Reprint button in
   the history — it starts a fresh job from the same source, but you still
   fill out the print details again (not one-click).

## Deploying on Unraid

Install the **Docker Compose** plugin first (one-time, via the Unraid web UI).

**Fresh install** (your `.env` is created from the example; edit it after):

```bash
cd /mnt/user/appdata && \
git clone https://github.com/ErRoR7/u1-print-bridge.git && \
cd u1-print-bridge && \
cp .env.example .env && \
docker compose up -d --build
```

Key `.env` values:
- `PRINTER_URL` — Moonraker on the U1 (default `http://192.168.68.242:7125`).
- `SPOOLMAN_URL` — your Spoolman (default `http://192.168.68.84:7912`).
- `NTFY_TOPIC` — ntfy topic for bridge alerts on your phone
  (default `u1-print-bridge`; subscribe in the ntfy app).

**Update to the latest from GitHub** (safe to re-run; your `.env` is kept):

```bash
cd /mnt/user/appdata && \
cp u1-print-bridge/.env /tmp/pb-env.bak 2>/dev/null; \
rm -rf u1-print-bridge && \
git clone https://github.com/ErRoR7/u1-print-bridge.git && \
cd u1-print-bridge && \
(cp /tmp/pb-env.bak .env 2>/dev/null || cp .env.example .env) && \
docker compose up -d --build
```

Then open `http://<unraid-ip>:8080` on your phone (same Wi-Fi).

## Self-test

Open `http://<unraid-ip>:8080/api/health` in a browser (or tap the pill at the
top of the app). You get one entry per service:

- **moonraker (Printer)** — green means the bridge can reach the U1's Moonraker
  and read its state. Red means the printer is unreachable (off, wrong IP, or
  network issue) — prints can't start until this is green.
- **spoolman (Spoolman)** — green means the spool inventory loaded. Red means
  the app can't read what's loaded in the slots; the color-check screen will
  say "spool info unavailable" until this is green.
- **ntfy (Alerts)** — green means bridge notifications are configured and the
  ntfy server is reachable. If `NTFY_TOPIC` is empty this shows as disabled
  (bridge alerts off — the app itself still works fine). It defaults to
  `u1-print-bridge`.

## Phone setup

Two free apps, about five minutes, one time:

1. **OctoEverywhere** — already linked to the U1. Printer alerts
   (started / complete / failed / paused, AI failure detection).
2. **ntfy** — install the app and subscribe to the topic you put in
   `NTFY_TOPIC` (e.g. `u1-print-bridge`). Bridge alerts: download failed,
   slicing failed, filament too low for the job, print finished.

## The color-slot model

The mapping is **positional**: 3MF filament slot N always prints on toolhead
T(N−1) — slot 1 → T0, slot 2 → T1, slot 3 → T2, slot 4 → T3. There is no
software re-mapping of painted colors in v1. The app reads what's actually
loaded on each toolhead straight from the printer (PAXX `print_task_config`,
the same data Fluidd's AFC panel shows) and shows it on the color-check
screen, so you just pick which toolhead prints each color. Spoolman is used
read-only for spool names and remaining grams; naming spool locations
`T0`–`T3` is an optional fallback for firmware without `print_task_config`.

## Notifications split

- **OctoEverywhere** = printer events: print started / complete / failed /
  paused, AI failure detection. (Already linked — nothing to build.)
- **ntfy** = bridge pipeline events: model download failed, slicing failed,
  filament too low for the job, print finished. Configure via `NTFY_TOPIC`
  in `.env`.

## Spoolman is read-only

The bridge only **reads** Spoolman (what's loaded, colors, grams left). The
printer itself tracks and deducts filament usage — the bridge never writes
to Spoolman, so there's exactly one writer and the numbers can't drift.

## Troubleshooting

- **Moonraker shows red / offline.** Check the printer is on and
  `PRINTER_URL` in `.env` matches its IP. PAXX firmware auth status is
  unknown — if Moonraker answers 401, the bridge's health detail will say so
  and credentials will need to be configured (see `backend/README.md`).
- **OrcaSlicer crash during slicing.** The backend retries automatically with
  a minimal-3MF fallback; check the job's "Details" log for what happened.
- **MakerWorld download fails.** MakerWorld is behind Cloudflare and may block
  server-side downloads. The job will ask you to download the 3MF in your own
  browser and use "Attach the 3MF file" (or the upload option) — still one
  button after that. Pasting a session cookie into `MAKERWORLD_SESSION_COOKIE`
  sometimes helps; see `.env.example`.
- **Spoolman locations (optional).** The color-check screen reads loaded
  filaments from the printer itself, so location naming isn't required.
  Naming spool locations `T0`–`T3` is only a fallback for firmware that
  doesn't expose `print_task_config`.
- **Spool info unavailable.** The app shows this when Spoolman can't be
  reached (its health entry will be red). Check `SPOOLMAN_URL` in `.env`.

## Attribution

- Upstream OrcaSlicer (AGPL) v2.4.0 — downloaded at slicer image build time;
  bundles the U1 machine/process/filament profiles used for slicing.
  (Switched from the Snapmaker fork 2026-10-10: the fork's CLI segfaults on
  3MF + --load-settings/--load-filaments.)
- `3mf-to-glb` (MIT) — used for format knowledge in thumbnail/3MF handling.
- Spoolman — filament inventory, the source of truth for loaded spools.
- OctoEverywhere — printer notifications and AI failure detection.

## Not in v1

- A print queue (if the printer is busy, the app tells you and you try again
  later — it never interrupts a running print).
- Starting prints from outside the LAN (no remote start without VPN/Tailscale;
  remote *monitoring* is already covered by OctoEverywhere).
- Software re-mapping of brush-painted colors — v1 uses positional mapping
  (slot N → toolhead T(N−1)) and the confirm screen instead.
