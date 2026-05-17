"""One-time CT2 conversion of HF Whisper finetunes (host-only).

Community Czech finetunes on Hugging Face (e.g. `mikr/whisper-large-v3-turbo-cs-*`)
ship as plain Transformers checkpoints, not CTranslate2. faster-whisper needs
CT2. We do a one-time conversion via `ct2-transformers-converter` (installed
with `uv sync --extra convert`, which pulls in transformers + torch).

The converted model goes under `data/cache/whisper-ct2/<flattened-hf-name>/`,
which is on the bind-mounted data dir so the container picks it up
automatically — no need to install torch inside the container.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def converted_dir(hf_name: str, ct2_root: Path) -> Path:
    """Where the CT2 version of `hf_name` lives (whether or not it exists yet)."""
    return ct2_root / hf_name.replace("/", "--")


def is_converted(hf_name: str, ct2_root: Path) -> bool:
    return (converted_dir(hf_name, ct2_root) / "model.bin").exists()


def convert(hf_name: str, ct2_root: Path, quantization: str = "int8") -> Path:
    """Convert HF model → CT2 under `ct2_root/<flat-name>/`. Returns the output dir."""
    if shutil.which("ct2-transformers-converter") is None:
        raise RuntimeError(
            "ct2-transformers-converter not found on PATH.\n"
            "Install host extras first:  uv sync --extra convert"
        )
    out_dir = converted_dir(hf_name, ct2_root)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    # If a previous attempt half-finished, clear it so --force gets a clean slate.
    if out_dir.exists() and not is_converted(hf_name, ct2_root):
        shutil.rmtree(out_dir)
    log.info("converting %s → %s (quantization=%s)", hf_name, out_dir, quantization)
    subprocess.run(
        [
            "ct2-transformers-converter",
            "--model", hf_name,
            "--output_dir", str(out_dir),
            "--copy_files", "tokenizer.json", "preprocessor_config.json",
            "--quantization", quantization,
            "--force",
        ],
        check=True,
    )
    return out_dir
