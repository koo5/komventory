"""Runtime configuration. All paths resolve from $KOMVENTORY_DATA (default ./data).

Inside the container, compose.yml sets KOMVENTORY_DATA=/data and bind-mounts the host ./data there.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

# Timezone used for entry timestamps. Host and phone both run Europe/Prague
# (CET/CEST auto-switch), so this is what filenames like
# `VID_20260515_122612.444.mp4` should be interpreted in.
TIMEZONE = ZoneInfo(os.environ.get("KOMVENTORY_TIMEZONE", "Europe/Prague"))


@dataclass(frozen=True)
class Paths:
    data: Path
    log_dir: Path
    log_md: Path
    stream_md: Path
    media: Path
    inbox: Path
    inbox_audio: Path
    inbox_video: Path
    inbox_openclaw: Path
    inbox_imports: Path
    inbox_pwa: Path
    cache_whisper: Path
    cache_piper: Path


def _inbox_subdir(inbox: Path, name: str, env_var: str) -> Path:
    """Resolve an inbox subdir from $env_var if set, else inbox/<name>.

    The default path may itself be a symlink on the host — that's fine, the
    walker resolves through it. Use the env-var override when running in
    Docker (where bind mounts of phone-sync dirs are easier than symlinks).
    """
    override = os.environ.get(env_var)
    return Path(override).resolve() if override else inbox / name


def load_paths() -> Paths:
    data = Path(os.environ.get("KOMVENTORY_DATA", "data")).resolve()
    log_dir = data / "log"
    inbox = data / "inbox"
    return Paths(
        data=data,
        log_dir=log_dir,
        log_md=log_dir / "log.md",
        stream_md=log_dir / "stream.md",
        media=log_dir / "media",
        inbox=inbox,
        inbox_audio=_inbox_subdir(inbox, "audio", "KOMVENTORY_INBOX_AUDIO"),
        inbox_video=_inbox_subdir(inbox, "video", "KOMVENTORY_INBOX_VIDEO"),
        inbox_openclaw=_inbox_subdir(inbox, "openclaw", "KOMVENTORY_INBOX_OPENCLAW"),
        inbox_imports=_inbox_subdir(inbox, "imports", "KOMVENTORY_INBOX_IMPORTS"),
        inbox_pwa=_inbox_subdir(inbox, "pwa", "KOMVENTORY_INBOX_PWA"),
        cache_whisper=data / "cache" / "whisper",
        cache_piper=data / "cache" / "piper",
    )


# Multilingual default (notes are mostly Czech). Set KOMVENTORY_WHISPER_LANG=cs
# to skip language detection on each clip.
WHISPER_MODEL = os.environ.get("KOMVENTORY_WHISPER_MODEL", "large-v3")
WHISPER_DEVICE = os.environ.get("KOMVENTORY_WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.environ.get("KOMVENTORY_WHISPER_COMPUTE", "int8")
WHISPER_LANG = os.environ.get("KOMVENTORY_WHISPER_LANG") or None  # None → auto-detect

VIDEO_FRAME_INTERVAL_S = float(os.environ.get("KOMVENTORY_FRAME_INTERVAL_S", "5"))

# Default Piper voice for /api/tts. "thomcles-high" is jirka-medium fine-tuned
# on Thomcles/Czech-Speech-Monospeaker; "cs_CZ-jirka-medium" remains selectable
# per-request via the `voice` field for A/B comparison.
TTS_VOICE = os.environ.get("KOMVENTORY_TTS_VOICE", "thomcles-high")

# Git remote/branch used by sync.pull(). Explicit (rather than relying on upstream
# tracking) so the pull doesn't fail just because no `branch.<x>.merge` is set.
GIT_REMOTE = os.environ.get("KOMVENTORY_GIT_REMOTE", "origin")
GIT_BRANCH = os.environ.get("KOMVENTORY_GIT_BRANCH", "main")

AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".ogg", ".opus", ".flac", ".webm", ".aac"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}
NOTE_EXTS = {".md", ".txt", ".note"}
