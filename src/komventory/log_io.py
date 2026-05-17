"""Reader/writer for log.md. Single source of truth for entry format.

Entries insert chronologically by their `## <ISO>` header timestamp, not as
strict append: log.md is the human-browsable view; out-of-order arrivals
(backlog imports, late phone-sync) should land in the right place. Writes are
atomic via same-dir tempfile + os.replace. Cross-process serialisation is
provided by `lock.komventory_lock` at the CLI/watch boundary, not in here —
callers must hold the lock before calling `insert_entry`.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_HEADER_RE = re.compile(
    r"^## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?)\b",
    re.MULTILINE,
)


@dataclass
class Entry:
    timestamp: datetime
    source: str
    body: str
    loc: str | None = None
    attachments: list[str] = field(default_factory=list)

    def render(self) -> str:
        ts = self.timestamp.isoformat(timespec="seconds")
        header = f"## {ts} — source: {self.source}"
        if self.loc:
            header += f' — loc: "{self.loc}"'
        lines = [header, "", self.body.strip()]
        if self.attachments:
            lines.append("")
            for path in self.attachments:
                lines.append(f"![[{path}]]")
        lines.append("")
        return "\n".join(lines) + "\n"


def _find_insert_offset(text: str, ts: datetime) -> int:
    """Return byte offset before the first header whose timestamp > ts.

    Returns len(text) if no later entry exists (i.e. append at the end).
    """
    for m in _HEADER_RE.finditer(text):
        try:
            existing = datetime.fromisoformat(m.group(1))
        except ValueError:
            continue
        if existing > ts:
            return m.start()
    return len(text)


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path via a same-dir tempfile + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".log.", suffix=".md.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def insert_entry(log_md: Path, entry: Entry) -> None:
    """Insert entry at the right chronological spot.

    Caller must hold `lock.komventory_lock` — this function does not serialise.
    """
    log_md.parent.mkdir(parents=True, exist_ok=True)
    current = log_md.read_text(encoding="utf-8") if log_md.exists() else ""
    rendered = entry.render()
    offset = _find_insert_offset(current, entry.timestamp)
    if offset == len(current):
        # Append path: keep one blank line of separation if file is non-empty.
        if current and not current.endswith("\n\n"):
            rendered = ("\n" if current.endswith("\n") else "\n\n") + rendered
        new_content = current + rendered
    else:
        # Splice in. Each entry already ends with a single trailing newline.
        if not rendered.endswith("\n\n"):
            rendered = rendered + "\n"
        new_content = current[:offset] + rendered + current[offset:]
    _atomic_write(log_md, new_content)


# Back-compat alias for callers still using the old name (gdoc importer etc.).
append_entry = insert_entry
