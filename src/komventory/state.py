"""Processed-file ledger for read-only inbox dirs (phone-sync sources).

Phone-synced inboxes are mounted read-only, so we can't delete sources after
ingest. Instead we record `(size, mtime)` per file in a JSON ledger and skip
on subsequent runs. Keys are `<subdir>/<rel-under-subdir>` — independent of
where the inbox subdir physically lives, so a host with the source at
`/home/koom/d/sync/...` and a container with it mounted at `/data/inbox/audio`
share the same ledger.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from . import config


class _Record(TypedDict, total=False):
    size: int
    mtime: float
    ingested_at: str
    source_tag: str


class ProcessedLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, _Record] = {}
        self.reload()

    def reload(self) -> None:
        """Re-read from disk; another process may have marked entries since load.

        Call under the komventory lock before is_processed/mark so a long-lived
        ledger (the watcher's) doesn't act on — or save over — stale state.
        Keeps in-memory data if the file is missing or corrupt/mid-write.
        """
        if not self.path.exists():
            return
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    def is_processed(self, key: str, file: Path) -> bool:
        rec = self.data.get(key)
        if not rec:
            return False
        try:
            st = file.stat()
        except FileNotFoundError:
            return True
        return rec.get("size") == st.st_size and abs(rec.get("mtime", 0) - st.st_mtime) < 1.0

    def mark(self, key: str, file: Path, source_tag: str) -> None:
        try:
            st = file.stat()
        except FileNotFoundError:
            return
        self.data[key] = _Record(
            size=st.st_size,
            mtime=st.st_mtime,
            ingested_at=datetime.now(tz=config.TIMEZONE).isoformat(timespec="seconds"),
            source_tag=source_tag,
        )
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".processed.", suffix=".json.tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, sort_keys=True, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
