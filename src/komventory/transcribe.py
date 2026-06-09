"""faster-whisper wrapper. Lazy-loads the model so CLI startup stays fast."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from . import cleanup, config, model_convert


def _resolve_model_path(name: str, cache_root: Path) -> str:
    """Either pass `name` through (Systran-published model, faster-whisper auto-downloads)
    or resolve a `org/repo` HF name to a locally-converted CT2 directory.

    On a cache miss for an HF finetune we auto-convert *if* the converter is on
    PATH — i.e. on the host, which carries torch via `--extra convert`. The
    container ships without torch by design (model_convert.py), so there this
    stays a clear, actionable error pointing at the host-side build instead.
    """
    if "/" not in name:
        return name
    ct2_root = cache_root / "ct2"
    if not model_convert.is_converted(name, ct2_root):
        if model_convert.can_convert():
            model_convert.convert(name, ct2_root)
        else:
            raise FileNotFoundError(
                f"HF model {name!r} has no local CT2 copy, and the converter isn't\n"
                f"available here (the container ships without torch by design).\n"
                f"Build the cache once on the host:\n"
                f"  scripts/warm-whisper-cache.fish\n"
                f"  # or: uv run --extra convert komventory convert-model {name}\n"
                f"It writes data/cache/whisper/ct2/, which the container reads via\n"
                f"the bind mount — then restart the container."
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
    segments, info = _model().transcribe(
        str(audio_path),
        language=config.WHISPER_LANG,
        vad_filter=True,
    )
    # Hallucinated segments ("Děkujeme.", subtitle credits) often trail real
    # speech in the same clip, so filter per segment, not on the joined text.
    lang = config.WHISPER_LANG or info.language
    parts = [
        text
        for text in (seg.text.strip() for seg in segments)
        if text and not cleanup.should_ignore_transcription(text, lang=lang)
    ]
    return cleanup.remove_repetitions(" ".join(parts))
