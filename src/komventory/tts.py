"""Server-side Czech TTS via Piper.

Why server-side: Android browser SpeechSynthesis Czech voices are inconsistent
(quality varies by device, sometimes absent entirely). Piper with a Czech voice
runs fast on CPU and gives a predictable result.

Voice files are downloaded once on first use into data/cache/piper/<voice>/.
That cache dir is in the bind-mounted ./data volume, so the host sees them and
they survive container rebuilds.
"""

from __future__ import annotations

import io
import logging
import urllib.request
import wave
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

DEFAULT_VOICE = config.TTS_VOICE

# rhasspy/piper-voices on huggingface — canonical home for community Piper voices.
# Layout: <lang>/<locale>/<name>/<quality>/<voice>.onnx and .onnx.json
_HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# Voices hosted outside rhasspy/piper-voices. Short name → directory URL holding
# model.onnx + model.onnx.json. Both thomcles variants are cs_CZ-jirka-medium
# fine-tuned on Thomcles/Czech-Speech-Monospeaker ("Honza"). No license stated.
_CUSTOM_VOICES = {
    "thomcles-medium": "https://huggingface.co/Thomcles/Piper-TTS-Czech/resolve/main/medium",
    "thomcles-high": "https://huggingface.co/Thomcles/Piper-TTS-Czech/resolve/main/high",
}


def _voice_urls(voice: str) -> tuple[str, str]:
    """Return (onnx_url, json_url) for `voice`."""
    if voice in _CUSTOM_VOICES:
        base = _CUSTOM_VOICES[voice]
        return f"{base}/model.onnx", f"{base}/model.onnx.json"
    # e.g. cs_CZ-jirka-medium → cs/cs_CZ/jirka/medium/cs_CZ-jirka-medium
    locale, name, quality = voice.split("-")
    lang = locale.split("_")[0]
    base = f"{_HF_BASE}/{lang}/{locale}/{name}/{quality}/{voice}"
    return f"{base}.onnx", f"{base}.onnx.json"


def _ensure_voice(cache_dir: Path, voice: str) -> Path:
    onnx_url, json_url = _voice_urls(voice)
    voice_dir = cache_dir / voice
    voice_dir.mkdir(parents=True, exist_ok=True)
    onnx = voice_dir / f"{voice}.onnx"
    cfg = voice_dir / f"{voice}.onnx.json"
    for path, url in ((onnx, onnx_url), (cfg, json_url)):
        if path.exists() and path.stat().st_size > 0:
            continue
        log.info("downloading piper voice asset: %s", url)
        with urllib.request.urlopen(url, timeout=120) as r, open(path, "wb") as f:
            while chunk := r.read(1 << 16):
                f.write(chunk)
    return onnx


_voice_singleton: object | None = None
_voice_singleton_path: Path | None = None


def _load_voice(onnx_path: Path):
    """Load the Piper voice once per process. Importing piper is slow on cold start."""
    global _voice_singleton, _voice_singleton_path
    if _voice_singleton is not None and _voice_singleton_path == onnx_path:
        return _voice_singleton
    from piper import PiperVoice  # noqa: PLC0415 — import on first use only
    _voice_singleton = PiperVoice.load(str(onnx_path))
    _voice_singleton_path = onnx_path
    return _voice_singleton


def synthesize_wav(text: str, voice: str = DEFAULT_VOICE, paths: config.Paths | None = None) -> bytes:
    """Synthesise `text` to a WAV byte string at the voice's native sample rate.

    First call per voice downloads the model files (~60–90MB) and loads the Piper
    voice into memory; subsequent calls reuse the loaded model.
    """
    paths = paths or config.load_paths()
    onnx_path = _ensure_voice(paths.cache_piper, voice)
    piper_voice = _load_voice(onnx_path)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        piper_voice.synthesize_wav(text, wav)
    return buf.getvalue()
