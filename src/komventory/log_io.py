"""Append-only writer for log.md. Single source of truth for entry format."""

from __future__ import annotations

import fcntl
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


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
            # Quote to keep parsing simple even when locations contain spaces or punctuation.
            header += f' — loc: "{self.loc}"'
        lines = [header, "", self.body.strip()]
        if self.attachments:
            lines.append("")
            for path in self.attachments:
                lines.append(f"![[{path}]]")
        lines.append("")
        return "\n".join(lines) + "\n"


def append_entry(log_md: Path, entry: Entry) -> None:
    log_md.parent.mkdir(parents=True, exist_ok=True)
    rendered = entry.render()
    with log_md.open("a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            if f.tell() > 0:
                f.write("\n")
            f.write(rendered)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
