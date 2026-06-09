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
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, log_io

log = logging.getLogger(__name__)

# Active content-language pack (question words, prompt, Q/A labels). See lang.py.
LANG = config.LANG

_DIACRITIC_MAP = str.maketrans(
    "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ",
    "acdeeinorstuuyzACDEEINORSTUUYZ",
)

QA_MODEL = os.environ.get("KOMVENTORY_QA_MODEL", "gemini/gemini-2.5-flash")
SECRETS_DIR = Path(os.environ.get("KOMVENTORY_SECRETS_DIR", "/run/secrets"))

# We run the retry loop ourselves (instead of LiteLLM's opaque num_retries) so
# each transient failure's actual reason hits the log, and so permanent errors
# (auth, bad request) fail fast instead of burning the whole budget.
QA_MAX_ATTEMPTS = 3
QA_RETRY_BASE_S = 1.0  # exponential: 1s, 2s, … between attempts

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
    return first in LANG.question_words


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


_SYSTEM_PROMPT = LANG.system_prompt


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

    Owns its retry loop so each transient failure's real reason is logged (a
    bare LiteLLM `num_retries` swallows them — you only learn it hiccupped from
    the latency). Transient errors (rate-limit, overload, timeout, connection)
    are retried with exponential backoff; everything else (auth, bad request,
    context-window) is permanent and returned immediately.
    """
    _hydrate_secret_into_env(QA_MODEL)
    var = _required_key_var(QA_MODEL)
    if var and not os.environ.get(var):
        return None, f"LLM not configured: {var} not set and no /run/secrets/{var}"

    import litellm  # noqa: PLC0415 — defer cold-start
    from litellm import completion

    # Drop LiteLLM's "Give Feedback / Get Help …" banner it prints on every
    # caught exception (the actual error stays hidden behind _turn_on_debug()).
    litellm.suppress_debug_info = True

    transient = (
        litellm.RateLimitError,
        litellm.InternalServerError,
        litellm.ServiceUnavailableError,
        litellm.Timeout,
        litellm.APIConnectionError,
    )

    user_msg = (
        f"<log>\n{_simplify_log_for_grounding(log_md_text)}\n</log>\n\n"
        f"{LANG.question_label}: {text}\n\n"
        f"{LANG.answer_label}:"
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    for attempt in range(1, QA_MAX_ATTEMPTS + 1):
        try:
            # num_retries=0: we own the loop so each attempt's failure is visible.
            resp = completion(
                model=QA_MODEL, messages=messages, temperature=0.2,
                num_retries=0, timeout=30,
            )
        except transient as e:
            short = _short_error(e)
            if attempt < QA_MAX_ATTEMPTS:
                delay = QA_RETRY_BASE_S * 2 ** (attempt - 1)
                log.warning(
                    "LLM transient failure (attempt %d/%d): %s — retrying in %.0fs",
                    attempt, QA_MAX_ATTEMPTS, short, delay,
                )
                time.sleep(delay)
                continue
            log.error("LLM failed after %d attempts: %s", QA_MAX_ATTEMPTS, short)
            return None, short
        except Exception as e:
            # Permanent — retrying won't help; surface it now without burning attempts.
            log.error("LLM call failed (non-retryable): %s", _short_error(e))
            return None, _short_error(e)

        try:
            return resp.choices[0].message.content.strip(), None
        except (AttributeError, IndexError, KeyError) as e:
            log.error("unexpected LLM response shape: %r", resp)
            return None, f"bad LLM response shape: {e}"

    return None, "LLM call failed"  # unreachable; loop always returns


def classify_and_answer(text: str, log_md_text: str = "") -> QAResult:
    if not _looks_like_question(text):
        return QAResult(is_question=False)
    answer, error = _call_llm(text, log_md_text)
    return QAResult(is_question=True, answer=answer, error=error)
