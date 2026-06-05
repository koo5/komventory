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
            # The Python attribute is `loc` for historical reasons; the
            # rendered field name is `where:` — a single mixed-meaning slot
            # for whatever weakly anchors this entry in space (container,
            # place, room, "Honzova garáž", whatever).
            header += f' — where: "{self.loc}"'
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


# log.md is kept read-only between writes (invariant) so that ad-hoc tools and
# hand-edits don't silently slip in while the watcher is mid-ingest. Writers go
# through the lock + this module, which restores the invariant after each write.
LOG_MD_READONLY_MODE = 0o444


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path via a same-dir tempfile + os.replace.

    Always leaves the destination at LOG_MD_READONLY_MODE on success so that
    log.md is read-only between writes regardless of how it got into the FS
    (fresh write, pull-from-remote, manual touch).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".log.", suffix=".md.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    try:
        os.chmod(path, LOG_MD_READONLY_MODE)
    except OSError:
        # Non-fatal: the write itself succeeded, just leaves perms wrong.
        pass


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


_HEADER_PARSE_RE = re.compile(
    r"^## (?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?)"
    r"(?:\s+—\s+source:\s+(?P<source>\S+))?"
    r'(?:\s+—\s+where:\s+"(?P<where>[^"]*)")?'
    r"\s*$",
    re.MULTILINE,
)

_ATTACHMENT_RE = re.compile(r"!\[\[([^\]]+)\]\]")


def iter_entries(text: str):
    """Yield Entry objects parsed from log.md content, in file order (chronological).

    Inverse of `Entry.render`. Tolerates unknown header decorations gracefully:
    anything between `## <ts>` and end-of-line is matched optionally. Body is
    the slice between this header and the next header (or EOF), with
    attachments lifted out into `entry.attachments` and stripped from `body`.
    """
    matches = list(_HEADER_PARSE_RE.finditer(text))
    for i, m in enumerate(matches):
        try:
            ts = datetime.fromisoformat(m.group("ts"))
        except ValueError:
            continue
        block_start = m.end()
        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[block_start:block_end].strip("\n")
        attachments = _ATTACHMENT_RE.findall(block)
        body = _ATTACHMENT_RE.sub("", block).strip()
        yield Entry(
            timestamp=ts,
            source=m.group("source") or "",
            body=body,
            loc=m.group("where"),
            attachments=attachments,
        )
