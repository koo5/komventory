"""Classify a transcribed utterance and (optionally) answer it.

Backend is LiteLLM so the model is just a string — swap providers by changing
$KOMVENTORY_QA_MODEL (default `gemini/gemini-2.5-flash`). API keys come from
the matching env var (GEMINI_API_KEY for gemini/*, ANTHROPIC_API_KEY for
anthropic/*, etc.). If the env var is missing we try
/run/secrets/<KEY_NAME> — compose mounts the host's secrets dir there.

Classifier still runs locally regardless — it's fast and lets the UI know
whether to even bother calling the LLM.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Czech question starters. Not exhaustive; we also fall back to '?' detection.
# Diacritics-stripped match because Whisper output is inconsistent on them and
# we don't want to miss a question just because the model dropped a háček.
_CZ_Q_WORDS = {
    "kde", "kdy", "kdo", "co", "jak", "jake", "jaky", "jaka", "jakou",
    "ktery", "ktera", "ktere", "kteri",
    "proc", "kolik", "cim", "cemu", "koho", "ceho", "komu", "cemu",
}

_DIACRITIC_MAP = str.maketrans(
    "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ",
    "acdeeinorstuuyzACDEEINORSTUUYZ",
)

QA_MODEL = os.environ.get("KOMVENTORY_QA_MODEL", "gemini/gemini-2.5-flash")
SECRETS_DIR = Path(os.environ.get("KOMVENTORY_SECRETS_DIR", "/run/secrets"))

# Map LiteLLM provider prefixes → the env var name they look up.
_PROVIDER_KEY_VARS = {
    "gemini/": "GEMINI_API_KEY",
    "anthropic/": "ANTHROPIC_API_KEY",
    "openai/": "OPENAI_API_KEY",
    "groq/": "GROQ_API_KEY",
    "openrouter/": "OPENROUTER_API_KEY",
}


@dataclass
class QAResult:
    is_question: bool
    answer: str | None  # None when not a question or when LLM declined


def _looks_like_question(text: str) -> bool:
    text = text.strip()
    if not text:
        return False
    if "?" in text:
        return True
    first = text.split(maxsplit=1)[0].lower().translate(_DIACRITIC_MAP)
    first = re.sub(r"[^\w]", "", first)
    return first in _CZ_Q_WORDS


def _required_key_var(model: str) -> str | None:
    for prefix, var in _PROVIDER_KEY_VARS.items():
        if model.startswith(prefix):
            return var
    return None


def _hydrate_secret_into_env(model: str) -> None:
    """If the provider key isn't in env but a secret file exists for it, load
    the file and set the env var so LiteLLM picks it up."""
    var = _required_key_var(model)
    if not var or os.environ.get(var):
        return
    secret_path = SECRETS_DIR / var
    if not secret_path.exists():
        return
    try:
        os.environ[var] = secret_path.read_text(encoding="utf-8").strip()
    except OSError as e:
        log.warning("could not read secret %s: %s", secret_path, e)


_SYSTEM_PROMPT = (
    "Jsi asistent ve skladovacím systému uživatele. Pod jménem 'log' najdeš "
    "chronologický seznam poznámek o tom, kde má co uloženo, většinou česky. "
    "Odpovídej stručně česky. Když odpověď v logu není, řekni to přímo — "
    "neimprovizuj. Cituj klíčové fragmenty doslova, ať uživatel ví, na jakou "
    "poznámku odkazuješ."
)


def _call_llm(text: str, log_md_text: str) -> str:
    """Call the configured LLM via LiteLLM and return plain text in Czech.

    Errors bubble up as strings to the caller (the API endpoint), which logs
    + sends them as the answer so the user sees what went wrong instead of
    a silent empty bubble.
    """
    _hydrate_secret_into_env(QA_MODEL)
    var = _required_key_var(QA_MODEL)
    if var and not os.environ.get(var):
        return f"(LLM not configured: {var} not set and no /run/secrets/{var})"

    # Local import: litellm cold-start is non-trivial; keep it out of api boot.
    from litellm import completion  # noqa: PLC0415

    user_msg = (
        f"<log>\n{log_md_text}\n</log>\n\n"
        f"Otázka: {text}\n\n"
        f"Odpověď:"
    )
    try:
        resp = completion(
            model=QA_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
        )
    except Exception as e:
        log.exception("LLM call failed")
        return f"(LLM error: {type(e).__name__}: {e})"
    try:
        return resp.choices[0].message.content.strip()
    except (AttributeError, IndexError, KeyError) as e:
        log.error("unexpected LLM response shape: %r", resp)
        return f"(LLM bad response shape: {e})"


def classify_and_answer(text: str, log_md_text: str = "") -> QAResult:
    if not _looks_like_question(text):
        return QAResult(is_question=False, answer=None)
    return QAResult(is_question=True, answer=_call_llm(text, log_md_text))
