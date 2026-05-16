"""Video frame extraction via ffmpeg."""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import config


def extract_frames(video_path: Path, out_dir: Path) -> list[Path]:
    """One frame every config.VIDEO_FRAME_INTERVAL_S seconds. Returns paths in order."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = out_dir / "frame-%04d.jpg"
    fps = 1.0 / config.VIDEO_FRAME_INTERVAL_S
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path),
        "-vf", f"fps={fps}",
        "-q:v", "4",
        str(pattern),
    ]
    subprocess.run(cmd, check=True)
    return sorted(out_dir.glob("frame-*.jpg"))


def extract_audio_track(video_path: Path, out_path: Path) -> Path:
    """Pull the audio track out of a video as WAV for Whisper."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    return out_path
