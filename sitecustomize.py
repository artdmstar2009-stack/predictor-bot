"""Runtime defaults for Render before bot.py reads environment variables.

Python imports this module automatically on startup when it is present on
sys.path. The bot sync code reads SYNC_LOOKAHEAD_DAYS at import time, so this
keeps Render deployments from only fetching today's football fixtures.
"""

from __future__ import annotations

import os


def _ensure_min_int(name: str, minimum: int) -> None:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else 0
    except ValueError:
        value = 0
    if value < minimum:
        os.environ[name] = str(minimum)


os.environ.setdefault("FOOTBALL_ENABLED", "1")
_ensure_min_int("SYNC_LOOKAHEAD_DAYS", 7)
