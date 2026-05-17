"""Best-effort timestamp resolution for ingested files.

Fallback chain:
  1. Parse the filename (Android camera/screenshot/recorder conventions).
  2. File mtime.
  3. datetime.now().

All returned datetimes are local-tz-aware so they serialise as
`YYYY-MM-DDTHH:MM:SS+HH:MM` and sort consistently with other entries.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from . import config

# IMG_20260515_121827.943.jpg / VID_20260515_122612.444.mp4 / PXL_/MVIMG_
_CAMERA = re.compile(
    r"^(?:VID|IMG|PXL|MVIMG)_(\d{8})_(\d{6})(?:[._](\d{1,6}))?",
    re.IGNORECASE,
)
# Screenshot_20260515-121905.png  (Android variants also use '_' as separator)
_SCREENSHOT = re.compile(r"^Screenshot[_-](\d{8})[-_](\d{6})", re.IGNORECASE)
# 20260516235653.aac — bare yyyymmddHHMMSS prefix
_BARE_14 = re.compile(r"^(\d{14})\b")


def _build(date8: str, time6: str, frac: str | None = None) -> datetime | None:
    try:
        y, m, d = int(date8[:4]), int(date8[4:6]), int(date8[6:8])
        H, M, S = int(time6[:2]), int(time6[2:4]), int(time6[4:6])
        us = 0
        if frac:
            us = int((frac + "000000")[:6])
        return datetime(y, m, d, H, M, S, us, tzinfo=config.TIMEZONE)
    except (ValueError, IndexError):
        return None


def parse_filename(name: str) -> datetime | None:
    if (m := _CAMERA.match(name)):
        return _build(m.group(1), m.group(2), m.group(3))
    if (m := _SCREENSHOT.match(name)):
        return _build(m.group(1), m.group(2))
    if (m := _BARE_14.match(name)):
        s = m.group(1)
        return _build(s[:8], s[8:14])
    return None


def resolve(path: Path) -> datetime:
    """Pick the best timestamp we can for this file."""
    if (dt := parse_filename(path.name)) is not None:
        return dt
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=config.TIMEZONE)
    except OSError:
        return datetime.now(tz=config.TIMEZONE)
