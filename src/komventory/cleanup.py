"""Bridge to the whisper cleanup module.

The canonical code lives in whisper_dictation's common/whisper_cleanup.py; a
vendored copy lives alongside this module (src/komventory/whisper_cleanup.py),
re-copied by hand when the upstream changes. Re-exported here so the rest of the
package keeps importing it as ``komventory.cleanup``.
"""

from __future__ import annotations

from .whisper_cleanup import (  # noqa: F401
    remove_repetitions,
    should_ignore_transcription,
)
