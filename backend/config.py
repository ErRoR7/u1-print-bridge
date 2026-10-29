"""Runtime configuration for the U1 print bridge backend.

All values come from the environment and are overridable at container/run time.
LAN defaults match Joel's home network; the bridge must never crash at startup
when integrations are unreachable -- health probes report them as red instead.

NOTE on Spoolman: usage is strictly READ-ONLY. The printer itself deducts
filament usage from Spoolman (printer-side tracking is in place), so this
bridge never writes to Spoolman. There is intentionally no deduct/write path
anywhere in this codebase.
"""
from __future__ import annotations

import os


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


class Config:
    # Moonraker on the Snapmaker U1 (PAXX firmware retains Moonraker)
    PRINTER_URL: str = _env("PRINTER_URL", "http://192.168.68.242:7125")
    # Spoolman inventory (READ-ONLY for this bridge)
    SPOOLMAN_URL: str = _env("SPOOLMAN_URL", "http://192.168.68.84:7912")
    # ntfy push alerts; empty topic disables sending (health reports disabled)
    NTFY_TOPIC: str = _env("NTFY_TOPIC", "u1-print-bridge")
    NTFY_SERVER: str = _env("NTFY_SERVER", "https://ntfy.sh")
    # Optional logged-in MakerWorld session cookie for best-effort downloads.
    # Never used to bypass login walls; absent cookie => manual-upload fallback.
    MAKERWORLD_SESSION_COOKIE: str = _env("MAKERWORLD_SESSION_COOKIE", "")
    # Job storage root (jobs.db + per-job dirs live here)
    DATA_DIR: str = _env("DATA_DIR", "/data")
    # Public base URL of this bridge, used for ntfy deep links; omit when empty
    BRIDGE_URL: str = _env("BRIDGE_URL", "")

    VERSION: str = "1.0.16"

    @classmethod
    def refresh(cls) -> None:
        """Re-read the environment (used by tests)."""
        cls.PRINTER_URL = _env("PRINTER_URL", "http://192.168.68.242:7125")
        cls.SPOOLMAN_URL = _env("SPOOLMAN_URL", "http://192.168.68.84:7912")
        cls.NTFY_TOPIC = _env("NTFY_TOPIC", "u1-print-bridge")
        cls.NTFY_SERVER = _env("NTFY_SERVER", "https://ntfy.sh")
        cls.MAKERWORLD_SESSION_COOKIE = _env("MAKERWORLD_SESSION_COOKIE", "")
        cls.DATA_DIR = _env("DATA_DIR", "/data")
        cls.BRIDGE_URL = _env("BRIDGE_URL", "")


config = Config
