# Slicer service: headless upstream OrcaSlicer + U1 profiles.
#
# SWITCHED 2026-10-10 from Snapmaker fork to upstream OrcaSlicer:
# - Fork's CLI segfaults on 3MF + --load-settings/--load-filaments (unfixable).
# - Upstream v2.4.0 bundles U1 profiles (PR #14305) and handles 3MFs cleanly.
# - Requires: Application metadata stripped from 3MF (segfault trigger),
#   profiles flattened with inherits intact (compat check), cli_config.json
#   patched with Snapmaker U1 (safety check).
#
# Asset verification (2026-10-10, via GitHub API):
#   repo: OrcaSlicer/OrcaSlicer, tag: v2.4.0
#   asset: OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.0.AppImage
#   binary: squashfs-root/bin/orca-slicer (NOT AppRun -- AppRun is the GUI!)
#   profiles: squashfs-root/resources/profiles/Snapmaker/{machine,process,filament}/
FROM python:3.12-slim

ARG ORCA_VERSION=2.4.0
ARG ORCA_TAG=v2.4.0
ARG ORCA_ASSET=OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.0.AppImage

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    ORCA_BIN=/opt/orca/squashfs-root/bin/orca-slicer \
    ORCA_MACHINE_PROFILE=/opt/orca/profiles/machine.json \
    ORCA_PROCESS_PROFILE=/opt/orca/profiles/process.json \
    ORCA_FILAMENT_DIR=/opt/orca/profiles

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       # X11 / GL stack for xvfb + the extracted AppImage
       libgl1 libopengl0 libegl1 libxkbcommon0 xvfb xauth \
       # Upstream-specific libs
       libglu1-mesa libmspack0 \
       # WebKitGTK + Chromium runtime set (AppImage links these)
       libwebkit2gtk-4.1-0 \
       libgtk-3-0 libnotify4 libnss3 libnspr4 libxss1 libxtst6 \
       libatspi2.0-0 libcups2 libdrm2 libxcomposite1 libxdamage1 \
       libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
       libxfixes3 libx11-xcb1 libxcb-dri3-0 \
       curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Pillow: resize 3MF thumbnails for G-code embedding (preview on U1 screen).
# python:3.12-slim is EXTERNALLY-MANAGED, so pip needs --break-system-packages.
RUN pip install --no-cache-dir --break-system-packages pillow numpy

# --- Upstream OrcaSlicer AppImage, extracted (no FUSE in containers) ---
RUN curl -fSL -o /tmp/orca.AppImage \
      "https://github.com/OrcaSlicer/OrcaSlicer/releases/download/${ORCA_TAG}/${ORCA_ASSET}" \
    && chmod +x /tmp/orca.AppImage \
    && mkdir -p /opt/orca && cd /opt/orca \
    && /tmp/orca.AppImage --appimage-extract \
    && rm /tmp/orca.AppImage \
    && test -x /opt/orca/squashfs-root/bin/orca-slicer

# --- Patch Bambu CLI safety config with Snapmaker U1 ---
# Upstream's "check printer cli safe params" segfaults if the printer isn't
# in resources/profiles/BBL/cli_config.json. Add U1 with A1-class limits.
RUN python3 - << 'PYEOF'
import json
p = "/opt/orca/squashfs-root/resources/profiles/BBL/cli_config.json"
d = json.load(open(p))
d["printer"]["Snapmaker U1"] = d["printer"]["Bambu Lab A1"]
json.dump(d, open(p, "w"), indent=2)
print("cli_config.json patched with Snapmaker U1")
PYEOF

# --- Flatten U1 profiles (vendor-aware inherits resolution) ---
# Headless CLI cannot resolve "inherits" stubs reliably; flatten to explicit
# profiles. KEEP the "inherits" key so the CLI populates the printer system
# name (required for the -17 compat check). Fix the junk printer_settings_id
# Snapmaker committed upstream ("MyToolChanger 0.4 nozzle - Copy").
RUN mkdir -p /opt/orca/profiles && python3 - << 'PYEOF'
import json
from pathlib import Path

bundle = Path("/opt/orca/squashfs-root/resources/profiles")

def find_profile(name, vendor="Snapmaker"):
    for p in (bundle / vendor).rglob("*.json"):
        try:
            d = json.loads(p.read_text())
            if d.get("name") == name or p.stem == name:
                return p
        except Exception:
            pass
    for p in bundle.rglob("*.json"):
        try:
            d = json.loads(p.read_text())
            if d.get("name") == name or p.stem == name:
                return p
        except Exception:
            pass
    return None

def flatten(src_path, vendor="Snapmaker"):
    d = json.loads(Path(src_path).read_text())
    chain, seen, cur = [d], {d.get("name")}, d
    while cur.get("inherits"):
        pn = cur["inherits"]
        pp = find_profile(pn, vendor)
        if not pp or pn in seen:
            print(f"  WARN: unresolved inherits '{pn}'")
            break
        pd = json.loads(pp.read_text())
        chain.append(pd)
        seen.add(pn)
        cur = pd
    merged = {}
    for c in reversed(chain):
        merged.update(c)
    # KEEP inherits for CLI system-name resolution; fix instantiation
    merged["instantiation"] = "true"
    merged["from"] = "user"
    return merged

B = bundle / "Snapmaker"
# NOTE (2026-10-10): exact upstream v2.4.0 filenames verified against the
# extracted AppImage bundle. "0.20 Standard ..." has NO "mm"; there is no
# "Generic ABS/ASA @U1 base.json" -- use the Snapmaker ABS/ASA profiles.
jobs = {
    "machine.json": B / "machine" / "Snapmaker U1 (0.4 nozzle).json",
    "process.json": B / "process" / "0.20 Standard @Snapmaker U1 (0.4 nozzle).json",
    "filament_PLA.json": B / "filament" / "Snapmaker PLA @U1.json",
    "filament_PETG.json": B / "filament" / "Snapmaker PETG @U1.json",
    "filament_ABS.json": B / "filament" / "Snapmaker ABS @U1.json",
    "filament_ASA.json": B / "filament" / "Snapmaker ASA @U1.json",
    # Quality presets for the UI quality picker (process.json stays the default).
    "process_draft.json": B / "process" / "0.28 Extra Draft @Snapmaker U1 (0.4 nozzle).json",
    "process_standard.json": B / "process" / "0.20 Standard @Snapmaker U1 (0.4 nozzle).json",
    "process_fine.json": B / "process" / "0.12 Fine @Snapmaker U1 (0.4 nozzle).json",
    "process_extrafine.json": B / "process" / "0.08 Extra Fine @Snapmaker U1 (0.4 nozzle).json",
}
# These three are load-bearing for --load-settings/--load-filaments: fail the
# build loudly if a source profile is missing instead of silently skipping it
# (a skipped write only surfaces later as "can not find setting file" on the
# printer host, which is exactly the 2026-10-10 exit-253 failure).
REQUIRED = {"machine.json", "process.json", "filament_PLA.json"}
out = Path("/opt/orca/profiles")
missing_required = []
for name, src in jobs.items():
    if not src.exists():
        print(f"  WARN: missing {src}, skipping")
        if name in REQUIRED:
            missing_required.append(name)
        continue
    flat = flatten(src)
    # Fix junk printer_settings_id committed upstream
    if flat.get("name") == "Snapmaker U1 (0.4 nozzle)":
        flat["printer_settings_id"] = "Snapmaker U1 (0.4 nozzle)"
    # Ensure process compat check passes (system name resolution)
    if name == "process.json" or name.startswith("process_"):
        cp = flat.get("compatible_printers") or []
        for v in ("Snapmaker U1 (0.4 nozzle)", "SM_U1", "fdm_U1"):
            if v not in cp:
                cp.append(v)
        flat["compatible_printers"] = cp
    (out / name).write_text(json.dumps(flat, indent=2))
    print(f"  {name}: {len(flat)} keys")
if missing_required:
    raise SystemExit(f"missing required profiles: {missing_required}")
PYEOF

COPY worker.py /slicer/worker.py

CMD ["python", "/slicer/worker.py"]
