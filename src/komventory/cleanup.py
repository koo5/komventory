"""Bridge to the shared whisper cleanup module.

The canonical code lives in whisper_dictation's common/whisper_cleanup.py —
one copy, edited in one place, used by both projects. $KOMVENTORY_COMMON
points at that directory (compose bind-mounts it to /app/common; on the host
the sibling checkout is found automatically). Without it, cleanup degrades to
a no-op so komventory still runs standalone.
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)

_DEFAULT_COMMON = "/home/koom/repos/koo5/whisper_dictation/0/whisper_dictation/common"
_common = os.environ.get("KOMVENTORY_COMMON") or _DEFAULT_COMMON
if _common not in sys.path:
    sys.path.insert(0, _common)

try:
    from whisper_cleanup import remove_repetitions, should_ignore_transcription  # noqa: F401
except ImportError:
    log.warning(
        "shared whisper_cleanup module not found in %s (set KOMVENTORY_COMMON "
        "to whisper_dictation's common/ dir) — transcription cleanup disabled",
        _common,
    )

    def should_ignore_transcription(text, lang=None):
        return False

    def remove_repetitions(text, min_repetitions=6, keep_repetitions=5):
        return text
