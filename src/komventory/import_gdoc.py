"""One-shot importer for the existing Google Doc (exported as Markdown).

The Doc is organized by location/container headings, not by time. Each top-level
`# heading` becomes one log entry with `loc:` set to the raw heading verbatim
(no normalization — user will hand-tweak the resulting log.md). Free prose
before the first heading becomes an entry with no `loc:`. Embedded base64
images are decoded into `log/media/imports/` and wikilinked from the body.

Timestamps are synthetic: order-preserving, one second apart, starting at the
2024 epoch below. The Doc's embedded date strings are intentionally not parsed
— per user direction, only relative order matters.
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .log_io import Entry, append_entry

log = logging.getLogger(__name__)

GDOC_EPOCH = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone(timedelta(hours=2)))

# Reference-style image definition at the bottom of the doc:
#   [image1]: <data:image/jpeg;base64,/9j/4AA...>
# Sometimes Docs wraps the URL in angle brackets, sometimes not.
_IMAGE_DEF_RE = re.compile(
    r"^\[([^\]]+)\]:\s*<?data:image/([A-Za-z0-9.+-]+);base64,([^\s>]+)>?\s*$",
    re.MULTILINE,
)

# Inline image reference: ![alt][image1]  (alt is usually empty)
_IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\[([^\]]+)\]")

_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _decode_images(text: str, src_name: str, media_root: Path) -> dict[str, str]:
    """Decode all [imageN]: data:... defs into files. Returns id → log-relative path."""
    media_root.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {}
    for m in _IMAGE_DEF_RE.finditer(text):
        img_id, fmt, b64 = m.group(1), m.group(2).lower(), m.group(3)
        ext = "jpg" if fmt == "jpeg" else fmt
        safe_id = re.sub(r"\W+", "_", img_id)
        out_path = media_root / f"{src_name}-{safe_id}.{ext}"
        try:
            out_path.write_bytes(base64.b64decode(b64))
        except Exception:
            log.warning("could not decode image %s", img_id)
            continue
        # Path used inside the log entry, relative to log_dir.
        mapping[img_id] = f"media/imports/{out_path.name}"
    return mapping


def _split_sections(text: str) -> list[tuple[str | None, str]]:
    """Yield (heading | None, body) chunks split on top-level `# ` headings."""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [(None, text.strip())] if text.strip() else []
    out: list[tuple[str | None, str]] = []
    if matches[0].start() > 0:
        pre = text[: matches[0].start()].strip()
        if pre:
            out.append((None, pre))
    for i, m in enumerate(matches):
        heading = m.group(1).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        out.append((heading, body))
    return out


def _rewrite_image_refs(body: str, image_map: dict[str, str]) -> str:
    def repl(m: re.Match[str]) -> str:
        rel = image_map.get(m.group(1))
        return f"![[{rel}]]" if rel else m.group(0)
    return _IMAGE_REF_RE.sub(repl, body)


def import_gdoc(md_path: Path, paths: config.Paths | None = None) -> int:
    paths = paths or config.load_paths()
    text = md_path.read_text(encoding="utf-8")
    src_name = md_path.stem

    image_map = _decode_images(text, src_name, paths.media / "imports")
    text_without_defs = _IMAGE_DEF_RE.sub("", text)
    sections = _split_sections(text_without_defs)

    count = 0
    for i, (heading, body) in enumerate(sections):
        body = _rewrite_image_refs(body, image_map).strip()
        if not body and not heading:
            continue
        entry = Entry(
            timestamp=GDOC_EPOCH + timedelta(seconds=i),
            source=f"gdoc-import@{md_path.name}#entry-{i:04d}",
            body=body or "(heading only — no body)",
            loc=heading,
        )
        append_entry(paths.log_md, entry)
        count += 1
    log.info("imported %d entries, %d images", count, len(image_map))
    return count
