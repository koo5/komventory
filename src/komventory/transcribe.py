"""faster-whisper wrapper. Lazy-loads the model so CLI startup stays fast."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from . import config


@lru_cache(maxsize=1)
def _model():
    from faster_whisper import WhisperModel

    paths = config.load_paths()
    paths.cache_whisper.mkdir(parents=True, exist_ok=True)
    return WhisperModel(
        config.WHISPER_MODEL,
        device=config.WHISPER_DEVICE,
        compute_type=config.WHISPER_COMPUTE,
        download_root=str(paths.cache_whisper),
    )


def transcribe(audio_path: Path) -> str:
    segments, _info = _model().transcribe(
        str(audio_path),
        language=config.WHISPER_LANG,
        vad_filter=True,
    )
    return " ".join(seg.text.strip() for seg in segments if seg.text.strip())
