"""faster-whisper wrapper. Lazy-loads the model so CLI startup stays fast."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from . import config, model_convert


def _resolve_model_path(name: str, cache_root: Path) -> str:
    """Either pass `name` through (Systran-published model, faster-whisper auto-downloads)
    or resolve a `org/repo` HF name to a locally-converted CT2 directory.

    Errors with clear remediation if an HF model name was given but no CT2 copy exists.
    """
    if "/" not in name:
        return name
    ct2_root = cache_root / "ct2"
    if not model_convert.is_converted(name, ct2_root):
        raise FileNotFoundError(
            f"HF model {name!r} has no local CT2 copy.\n"
            f"Run on the host (one-time):\n"
            f"  uv sync --extra convert\n"
            f"  uv run komventory convert-model {name}\n"
            f"After it finishes, restart the container."
        )
    return str(model_convert.converted_dir(name, ct2_root))


@lru_cache(maxsize=1)
def _model():
    from faster_whisper import WhisperModel

    paths = config.load_paths()
    paths.cache_whisper.mkdir(parents=True, exist_ok=True)
    model_path = _resolve_model_path(config.WHISPER_MODEL, paths.cache_whisper)
    return WhisperModel(
        model_path,
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
