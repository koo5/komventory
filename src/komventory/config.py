"""Runtime configuration. All paths resolve from $KOMVENTORY_DATA (default ./data).

Inside the container, compose.yml sets KOMVENTORY_DATA=/data and bind-mounts the host ./data there.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    data: Path
    log_dir: Path
    log_md: Path
    media: Path
    inbox: Path
    inbox_audio: Path
    inbox_video: Path
    inbox_openclaw: Path
    inbox_imports: Path
    cache_whisper: Path


def load_paths() -> Paths:
    data = Path(os.environ.get("KOMVENTORY_DATA", "data")).resolve()
    log_dir = data / "log"
    inbox = data / "inbox"
    return Paths(
        data=data,
        log_dir=log_dir,
        log_md=log_dir / "log.md",
        media=log_dir / "media",
        inbox=inbox,
        inbox_audio=inbox / "audio",
        inbox_video=inbox / "video",
        inbox_openclaw=inbox / "openclaw",
        inbox_imports=inbox / "imports",
        cache_whisper=data / "cache" / "whisper",
    )


# Multilingual default (notes are mostly Czech). Set KOMVENTORY_WHISPER_LANG=cs
# to skip language detection on each clip.
WHISPER_MODEL = os.environ.get("KOMVENTORY_WHISPER_MODEL", "medium")
WHISPER_DEVICE = os.environ.get("KOMVENTORY_WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.environ.get("KOMVENTORY_WHISPER_COMPUTE", "int8")
WHISPER_LANG = os.environ.get("KOMVENTORY_WHISPER_LANG") or None  # None → auto-detect

VIDEO_FRAME_INTERVAL_S = float(os.environ.get("KOMVENTORY_FRAME_INTERVAL_S", "5"))

AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".ogg", ".opus", ".flac", ".webm"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}
NOTE_EXTS = {".md", ".txt", ".note"}
