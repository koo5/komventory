"""Content-language packs, selected by $KOMVENTORY_LANG (default 'en').

The UI chrome (buttons, status messages) is always English. This module only
governs *content*: the LLM system prompt, the question/answer framing, the
question-word heuristic, the Whisper transcription language, and the default
TTS voice. $KOMVENTORY_LANG is the single language switch — there's no separate
"whisper language" knob, because the spoken language, the answer language, and
the spoken-back language are all the same thing. The default voice it picks
stays overridable via $KOMVENTORY_TTS_VOICE (voice choice is a separate axis
from language: e.g. cs_CZ-jirka vs the thomcles fine-tunes).

Add a language by appending a LangPack to PACKS. Unknown $KOMVENTORY_LANG values
fall back to English so a typo degrades gracefully rather than crashing.

This module reads $KOMVENTORY_LANG directly (not via config) to avoid an import
cycle: config imports lang to derive its Whisper/TTS defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class LangPack:
    code: str
    # LLM system prompt — defines persona, grounding, and answer language.
    system_prompt: str
    # Labels framing the grounded user message: "<question_label>: ...".
    question_label: str
    answer_label: str
    # Lowercased, diacritics-stripped question starters for the cheap local
    # "is this a question?" heuristic (we also fall back to '?' detection).
    question_words: frozenset
    # Whisper transcription language for this content language.
    whisper_lang: str
    # Default TTS voice (a Piper voice name); $KOMVENTORY_TTS_VOICE overrides it.
    tts_voice: str


_EN = LangPack(
    code="en",
    system_prompt=(
        "You are an assistant in the user's storage/inventory system. Under the "
        "name 'log' you'll find a chronological list of notes about where they "
        "keep what. Each log line has the form `[date time] note body`. Answer "
        "concisely in English. If the answer isn't in the log, say so directly — "
        "don't make things up. If you need to quote a note, use a short quote "
        "from the body and a brief date (e.g. \"a week ago\"), not full ISO "
        "timestamps."
    ),
    question_label="Question",
    answer_label="Answer",
    question_words=frozenset(
        {"where", "when", "who", "whom", "whose", "what", "which", "how", "why"}
    ),
    whisper_lang="en",
    tts_voice="en_US-lessac-medium",
)

_CS = LangPack(
    code="cs",
    system_prompt=(
        "Jsi asistent ve skladovacím systému uživatele. Pod jménem 'log' najdeš "
        "chronologický seznam poznámek o tom, kde má co uloženo, většinou česky. "
        "Každý řádek logu má tvar `[datum čas] tělo poznámky`. "
        "Odpovídej stručně česky. Když odpověď v logu není, řekni to přímo — "
        "neimprovizuj. Pokud potřebuješ poznámku odcitovat, použij krátkou citaci "
        "z těla a stručné datum (např. „před týdnem“), ne celá ISO timestamps."
    ),
    question_label="Otázka",
    answer_label="Odpověď",
    # Diacritics-stripped match because Whisper output is inconsistent on them
    # and we don't want to miss a question just because the model dropped a háček.
    question_words=frozenset(
        {
            "kde", "kdy", "kdo", "co", "jak", "jake", "jaky", "jaka", "jakou",
            "ktery", "ktera", "ktere", "kteri",
            "proc", "kolik", "cim", "cemu", "koho", "ceho", "komu",
        }
    ),
    whisper_lang="cs",
    tts_voice="cs_CZ-jirka-medium",
)

PACKS = {p.code: p for p in (_EN, _CS)}
_DEFAULT = _EN


def current_code() -> str:
    return os.environ.get("KOMVENTORY_LANG", _DEFAULT.code).strip().lower()


def active() -> LangPack:
    """The pack for $KOMVENTORY_LANG, falling back to English on unknown codes."""
    return PACKS.get(current_code(), _DEFAULT)
