"""Render log.md to a standalone, browsable log.html.

Output lives next to log.md (default) so the relative `media/...` paths in
entries resolve naturally when opened in a browser. Phone clips/frames inline
as `<video>`/`<audio>`/`<img>` based on file extension; the body Markdown is
converted with the `markdown` package and lightly themed.
"""

from __future__ import annotations

import html
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

import markdown as md

from . import config

_ENTRY_RE = re.compile(
    r"^## (?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?) "
    r"— source: (?P<source>\S+)"
    # Accept legacy `loc:` and current `where:` — they mean the same thing.
    r'(?: — (?:loc|where): "(?P<where>[^"]+)")?\s*$',
    re.MULTILINE,
)

_WIKILINK_RE = re.compile(r"!\[\[([^\]]+)\]\]")

_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
_AUDIO_EXTS = {".aac", ".m4a", ".mp3", ".wav", ".ogg", ".opus", ".flac"}
# Everything else is treated as an image (jpg/png/gif/webp/heic/etc.).


def _wikilink_to_html(match: re.Match[str]) -> str:
    raw = match.group(1).strip()
    ext = Path(raw).suffix.lower()
    safe = html.escape(raw, quote=True)
    if ext in _VIDEO_EXTS:
        return f'<video controls preload="metadata" src="{safe}"></video>'
    if ext in _AUDIO_EXTS:
        return f'<audio controls preload="metadata" src="{safe}"></audio>'
    alt = html.escape(Path(raw).name, quote=True)
    # Extracted video frames live under `<video>.frames/frame-NNNN.jpg`. Mark them
    # so even a single one renders thumbnail-sized rather than competing with the
    # video tag above for vertical space.
    cls = ' class="frame"' if ".frames/" in raw else ""
    return f'<img loading="lazy"{cls} src="{safe}" alt="{alt}">'


# Two or more <img> tags separated only by whitespace / <br> get bundled into
# a horizontal scroll strip — turns the typical "12 video frames stacked" case
# into something you can actually scan.
_FRAME_STRIP_RE = re.compile(
    r"(?:<img\b[^>]+>(?:\s*<br\s*/?>)?\s*){2,}",
    re.IGNORECASE,
)


def _group_image_runs(html: str) -> str:
    def wrap(m: re.Match[str]) -> str:
        return f'<div class="frame-strip">{m.group(0)}</div>'
    return _FRAME_STRIP_RE.sub(wrap, html)


def _body_to_html(body: str) -> str:
    # Convert wikilinks first so the markdown processor doesn't touch their inner brackets.
    body = _WIKILINK_RE.sub(_wikilink_to_html, body)
    rendered = md.markdown(body, extensions=["extra", "sane_lists"], output_format="html5")
    return _group_image_runs(rendered)


def _parse_entries(text: str) -> list[dict]:
    matches = list(_ENTRY_RE.finditer(text))
    entries: list[dict] = []
    for i, m in enumerate(matches):
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip("\n")
        entries.append(
            {
                "ts": m.group("ts"),
                "source": m.group("source"),
                "where": m.group("where"),
                "body": body,
            }
        )
    return entries


_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<title>Komventory</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 60rem;
         margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }}
  h1 {{ margin: 0; }}
  .meta {{ color: #888; font-size: 0.9em; margin: 0 0 2rem; }}
  article {{ border-top: 1px solid #ccc4; padding: 1.25rem 0; }}
  article header {{ font-size: 0.85em; color: #777; display: flex; gap: 0.75em;
                    flex-wrap: wrap; margin-bottom: 0.4rem; }}
  article header time {{ font-variant-numeric: tabular-nums; color: #555; }}
  article header .where {{ color: #036; font-weight: 600; }}
  @media (prefers-color-scheme: dark) {{
    article header .where {{ color: #6cf; }}
  }}
  article header .source {{ font-family: ui-monospace, monospace; font-size: 0.9em;
                            color: #999; }}
  article .body p:first-child {{ margin-top: 0; }}
  /* Cap heights aggressively so portrait phone media doesn't take 2 pages each.
     Click to zoom (img) or fullscreen (video controls) to see full size. */
  img {{ max-width: 100%; max-height: 50vh; height: auto; display: block;
         margin: 0.5rem 0; border-radius: 4px; cursor: zoom-in; }}
  /* Extracted video frames stay thumbnail-sized whether grouped in a strip or alone. */
  img.frame {{ max-height: 180px; }}
  video {{ max-width: 100%; max-height: 50vh; height: auto; display: block;
           margin: 0.5rem 0; border-radius: 4px; }}
  audio {{ width: 100%; margin: 0.5rem 0; }}
  /* Group of 2+ consecutive images: horizontal scroll strip. */
  .frame-strip {{ display: flex; gap: 0.5rem; overflow-x: auto;
                  padding: 0.5rem 0; margin: 0.25rem 0;
                  scroll-snap-type: x proximity;
                  scrollbar-width: thin; }}
  .frame-strip img {{ height: 180px; width: auto; max-height: 180px;
                      flex: 0 0 auto; margin: 0; border-radius: 4px;
                      scroll-snap-align: start; cursor: zoom-in; }}
  /* Click-to-zoom lightbox. Pure CSS sizing; JS appends/removes the element. */
  .lightbox {{ position: fixed; inset: 0; background: #000c;
               display: grid; place-items: center; z-index: 1000;
               cursor: zoom-out; padding: 1rem; }}
  .lightbox img {{ max-width: 95vw; max-height: 95vh; width: auto; height: auto;
                   max-height: 95vh; cursor: zoom-out; box-shadow: 0 0 40px #0008; }}
  pre, code {{ font-family: ui-monospace, monospace; }}
</style>
<script>
  // Click any image → fullscreen lightbox. Click/ESC → close.
  document.addEventListener('click', e => {{
    const img = e.target.closest('article img');
    if (!img) return;
    e.preventDefault();
    const o = document.createElement('div');
    o.className = 'lightbox';
    const big = document.createElement('img');
    big.src = img.currentSrc || img.src;
    big.alt = img.alt;
    o.appendChild(big);
    o.addEventListener('click', () => o.remove());
    document.body.appendChild(o);
  }});
  document.addEventListener('keydown', e => {{
    if (e.key === 'Escape') document.querySelectorAll('.lightbox').forEach(n => n.remove());
  }});
</script>
</head>
<body>
<h1>Komventory</h1>
<p class="meta">{count} entries · rendered {rendered_at}</p>
{articles}
</body>
</html>
"""

_ARTICLE_TEMPLATE = """<article id="{anchor}">
<header>
  <time datetime="{ts}">{ts_display}</time>
  {where_html}
  <span class="source">{source}</span>
</header>
<div class="body">{body_html}</div>
</article>
"""


def _format_ts_display(ts: str) -> str:
    """Render the ISO timestamp as `YYYY-MM-DD HH:MM` for human reading; full ISO stays in @datetime."""
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    return dt.strftime("%Y-%m-%d %H:%M")


def render(log_md_path: Path, out_path: Path | None = None) -> Path:
    out_path = out_path or log_md_path.with_suffix(".html")
    text = log_md_path.read_text(encoding="utf-8")
    entries = _parse_entries(text)

    articles: list[str] = []
    for i, e in enumerate(entries):
        where_html = (
            f'<span class="where">{html.escape(e["where"], quote=True)}</span>'
            if e["where"]
            else ""
        )
        articles.append(
            _ARTICLE_TEMPLATE.format(
                anchor=f"e{i:05d}",
                ts=html.escape(e["ts"], quote=True),
                ts_display=html.escape(_format_ts_display(e["ts"]), quote=True),
                where_html=where_html,
                source=html.escape(e["source"], quote=True),
                body_html=_body_to_html(e["body"]),
            )
        )

    page = _PAGE_TEMPLATE.format(
        count=len(entries),
        rendered_at=datetime.now(tz=config.TIMEZONE).strftime("%Y-%m-%d %H:%M %Z"),
        articles="\n".join(articles),
    )
    # Atomic write: write to a sibling tempfile + os.replace. Caddy (or any
    # browser hitting log.html mid-render) sees either the old file or the new
    # file, never a half-written one.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".log.", suffix=".html.tmp", dir=str(out_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(page)
        os.replace(tmp, out_path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return out_path
