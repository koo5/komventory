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

from . import log_io

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
    answer: str | None = None  # set on success; None when not a question or on error
    error: str | None = None   # short transient/permanent failure message; client may retry


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
    "Každý řádek logu má tvar `[datum čas] tělo poznámky`. "
    "Odpovídej stručně česky. Když odpověď v logu není, řekni to přímo — "
    "neimprovizuj. Pokud potřebuješ poznámku odcitovat, použij krátkou citaci "
    "z těla a stručné datum (např. „před týdnem“), ne celá ISO timestamps."
)


def _simplify_log_for_grounding(text: str) -> str:
    """Strip log.md metadata before handing it to the LLM.

    The raw markdown carries `id: <ulid>`, `source: <kind>` and full ISO
    timestamps on every header — none of which is useful for answering, and
    the model tends to quote it back at the user. Reduce each entry to
    `[YYYY-MM-DD HH:MM] [where] body` so citations stay readable.
    """
    lines: list[str] = []
    for e in log_io.iter_entries(text):
        when = e.timestamp.strftime("%Y-%m-%d %H:%M")
        prefix = f"[{when}]"
        if e.loc:
            prefix += f" [{e.loc}]"
        lines.append(f"{prefix} {e.body}".strip())
    return "\n\n".join(lines)


def _short_error(e: Exception) -> str:
    """Compact, speakable error: `<code> <provider>: <message>`.

    LiteLLM normalises exceptions to carry `status_code` and `llm_provider`
    attributes; fall back to the class name when missing. The inner JSON
    `"message": "..."` field (Gemini/OpenAI/Anthropic-shaped errors) is
    preferred over the wrapping noise from str(e). First sentence only —
    avoids reading paragraphs of provider boilerplate aloud.
    """
    code = getattr(e, "status_code", None)
    provider = getattr(e, "llm_provider", None)
    raw = str(e) or type(e).__name__
    inner = re.search(r'"message"\s*:\s*"([^"]+)"', raw)
    msg = inner.group(1) if inner else raw.splitlines()[0]
    msg = msg.split(". ", 1)[0].strip().rstrip(".")
    head_parts: list[str] = []
    if code:
        head_parts.append(str(code))
    if provider:
        head_parts.append(provider)
    head = " ".join(head_parts) if head_parts else type(e).__name__
    return f"{head}: {msg}"[:240]


def _call_llm(text: str, log_md_text: str) -> tuple[str | None, str | None]:
    """Return (answer, error). At most one is non-None.

    LiteLLM handles transient retries internally via `num_retries`; we only
    see the failure after they're exhausted, which means it's worth surfacing
    to the user (they can retry by asking again).
    """
    _hydrate_secret_into_env(QA_MODEL)
    var = _required_key_var(QA_MODEL)
    if var and not os.environ.get(var):
        return None, f"LLM not configured: {var} not set and no /run/secrets/{var}"

    from litellm import completion  # noqa: PLC0415 — defer cold-start

    user_msg = (
        f"<log>\n{_simplify_log_for_grounding(log_md_text)}\n</log>\n\n"
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
            # LiteLLM handles 429/503/timeout-style transients internally and
            # only raises after exhaustion. Three tries with default backoff.
            num_retries=3,
            timeout=30,
        )
    except Exception as e:
        log.exception("LLM call failed after retries")
        return None, _short_error(e)
    try:
        return resp.choices[0].message.content.strip(), None
    except (AttributeError, IndexError, KeyError) as e:
        log.error("unexpected LLM response shape: %r", resp)
        return None, f"bad LLM response shape: {e}"


def classify_and_answer(text: str, log_md_text: str = "") -> QAResult:
    if not _looks_like_question(text):
        return QAResult(is_question=False)
    answer, error = _call_llm(text, log_md_text)
    return QAResult(is_question=True, answer=answer, error=error)
