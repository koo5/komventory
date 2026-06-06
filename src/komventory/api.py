"""FastAPI HTTP surface for the PWA: capture, browse, ask, speak.

Shape:
  GET  /                      — PWA index.html
  GET  /static/*              — PWA static assets
  GET  /api/log/recent?n=50   — recent entries as JSON
  GET  /api/log/stream        — SSE pings on log.md change (client refetches)
  POST /api/notes/text        — JSON {body, where?} → entry
  POST /api/notes/audio       — multipart file → transcribed entry
  POST /api/ask               — JSON {text} → {is_question, answer?}
  POST /api/tts               — JSON {text} → audio/wav

All write paths reuse `synced_lock` so they coexist with the file-watcher
container: lock → pull → mutate → render → commit → release.

Frontend dir resolution:
  $KOMVENTORY_FRONTEND if set, else <repo_root>/frontend. Inside docker we
  bind-mount the repo's frontend/ dir at /app/frontend so a fixed default
  works from the installed package too.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, ingest, log_io, qa, render_html, sync, tts
from .sync import synced_lock

log = logging.getLogger(__name__)


def _frontend_dir() -> Path:
    override = os.environ.get("KOMVENTORY_FRONTEND")
    if override:
        return Path(override).resolve()
    # src/komventory/api.py → src → repo_root
    return Path(__file__).resolve().parents[2] / "frontend"


def _entry_to_dict(e: log_io.Entry) -> dict:
    return {
        "id": e.id,
        "timestamp": e.timestamp.isoformat(timespec="milliseconds"),
        "source": e.source,
        "body": e.body,
        "where": e.loc,
        "attachments": list(e.attachments),
    }


class TextNote(BaseModel):
    body: str = Field(min_length=1)
    where: str | None = None


class AskRequest(BaseModel):
    text: str = Field(min_length=1)
    # Optional: if the question came from a stream entry, the answer entry
    # we persist will source-tag back at it (`gemini@<anchor_source>`) so the
    # PWA can pair them in the interleaved view.
    anchor_source: str | None = None


class TTSRequest(BaseModel):
    text: str = Field(min_length=1)
    voice: str | None = None


class PromoteRequest(BaseModel):
    # Preferred path: id alone uniquely identifies the entry.
    id: str | None = None
    # Legacy fallback for entries written before the ULID rollout.
    timestamp: str | None = None
    source: str | None = None


app = FastAPI(title="komventory", version="0.1.0")


# ---------------------------------------------------------------- static --
@app.get("/")
def index() -> FileResponse:
    return FileResponse(_frontend_dir() / "index.html")


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(_frontend_dir() / "manifest.webmanifest")


@app.get("/sw.js")
def service_worker() -> FileResponse:
    # Service workers need same-origin + correct MIME; FileResponse handles MIME.
    return FileResponse(_frontend_dir() / "sw.js", media_type="application/javascript")


# ------------------------------------------------------------------- log --
def _read_entries(path: Path) -> list[log_io.Entry]:
    if not path.exists():
        return []
    return list(log_io.iter_entries(path.read_text(encoding="utf-8")))


def _dedupe_key(e: log_io.Entry):
    """Key for stream/log merge. Prefer ULID id; legacy entries fall back to
    (timestamp_iso_ms, source) — pre-ULID rollout entries have no id."""
    return e.id if e.id else (e.timestamp.isoformat(timespec="milliseconds"), e.source)


def _merged_entries(paths: config.Paths) -> list[tuple[log_io.Entry, str]]:
    """Merge stream.md and log.md, dedupe via _dedupe_key.

    Log wins over stream on conflict — auto-promoted entries appear in both
    files identically (same id), so dedupe makes them collapse to a single
    "log"-tagged item in the interleaved view, with the promote button hidden.
    """
    seen: dict = {}
    # Order matters: stream first, log overwrites.
    for file_path, file_tag in ((paths.stream_md, "stream"), (paths.log_md, "log")):
        for e in _read_entries(file_path):
            seen[_dedupe_key(e)] = (e, file_tag)
    return sorted(seen.values(), key=lambda t: t[0].timestamp)


@app.get("/api/log/recent")
def log_recent(n: int = 50) -> JSONResponse:
    paths = config.load_paths()
    merged = _merged_entries(paths)
    return JSONResponse([
        {**_entry_to_dict(e), "file": tag} for (e, tag) in merged[-n:]
    ])


@app.get("/api/log/stream")
async def log_stream() -> StreamingResponse:
    """SSE: emit a `log-changed` event whenever log.md OR stream.md changes.

    Client receives the ping and refetches /api/log/recent — simple and robust
    against chronological inserts that can land anywhere in either file.
    """
    paths = config.load_paths()
    watched = (paths.log_md, paths.stream_md)

    async def gen():
        last: list[float | None] = [None, None]
        yield "event: hello\ndata: {}\n\n"
        while True:
            await asyncio.sleep(0.5)
            now = []
            for p in watched:
                try:
                    now.append(p.stat().st_mtime)
                except FileNotFoundError:
                    now.append(0.0)
            if last[0] is None:
                last = now
                continue
            if now != last:
                last = now
                yield "event: log-changed\ndata: {}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ----------------------------------------------------------------- notes --
@app.post("/api/notes/text")
def post_note_text(note: TextNote) -> JSONResponse:
    paths = config.load_paths()
    entry = log_io.Entry(
        timestamp=datetime.now(tz=config.TIMEZONE),
        source="text@pwa",
        body=note.body.strip(),
        id=log_io.new_id(),
        loc=note.where,
    )
    with synced_lock(paths, purpose="api-text"):
        # Routes via stream.md always; classifier auto-promotes non-questions
        # into log.md. Same rule used by ingest.commit_entry for audio/video.
        ingest.commit_entry(entry, paths, kind="text")
        _render_safe(paths)
        sync.commit_safe(paths.log_dir, "api: text note")
    # Tag with "log" or "stream" so the PWA shows the right badge / hides
    # the promote button when the entry was auto-promoted.
    file_tag = "log" if not qa._looks_like_question(entry.body) else "stream"
    return JSONResponse({**_entry_to_dict(entry), "file": file_tag})


@app.post("/api/notes/audio")
def post_note_audio(file: UploadFile = File(...)) -> JSONResponse:
    """Save uploaded audio under data/inbox/pwa/, then reuse ingest_one.

    Ingest classifies by extension and pwa is an 'owned' subdir, so the source
    file gets staged into media/pwa/ and unlinked from the inbox automatically.
    """
    paths = config.load_paths()
    src_name = file.filename or "audio.webm"
    ext = Path(src_name).suffix.lower() or ".webm"
    if ext not in config.AUDIO_EXTS and ext not in config.VIDEO_EXTS:
        raise HTTPException(status_code=415, detail=f"unsupported audio extension: {ext}")
    fname = f"{uuid.uuid4().hex}{ext}"
    paths.inbox_pwa.mkdir(parents=True, exist_ok=True)
    dest = paths.inbox_pwa / fname
    with dest.open("wb") as f:
        while chunk := file.file.read(1 << 20):
            f.write(chunk)
    # ingest_one transcribes WITHOUT the lock and locks only the mutation tail
    # (insert/render/commit), so concurrent text notes aren't blocked on Whisper.
    try:
        entry = ingest.ingest_one(dest, paths, commit_label="api: audio note")
    except Exception:
        log.exception("ingest_one failed for %s", dest)
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="ingest failed")
    if entry is None:
        # 204 forbids a body; an empty Response is the only legal shape.
        return Response(status_code=204)
    file_tag = "log" if not qa._looks_like_question(entry.body) else "stream"
    return JSONResponse({**_entry_to_dict(entry), "file": file_tag})


# -------------------------------------------------------------------- ask --
@app.post("/api/ask")
def post_ask(req: AskRequest) -> JSONResponse:
    paths = config.load_paths()
    # LLM grounds on log.md ONLY — stream.md noise (questions, half-formed
    # mumbling, LLM answers themselves) should never colour the answer.
    log_text = paths.log_md.read_text(encoding="utf-8") if paths.log_md.exists() else ""
    result = qa.classify_and_answer(req.text, log_text)
    answer_entry = None
    if result.is_question and result.answer and not result.error:
        # Persist the answer to stream.md so it survives page refresh. Tag it
        # back at the asking entry's source when we have one, otherwise just
        # `gemini@pwa` for direct /api/ask calls without a stream anchor.
        # Errors are NOT persisted — they're transient and would otherwise
        # pollute stream.md with junk like "ServiceUnavailableError: 503".
        anchor = req.anchor_source or "pwa"
        answer_entry = log_io.Entry(
            timestamp=datetime.now(tz=config.TIMEZONE),
            source=f"gemini@{anchor}",
            body=result.answer,
            id=log_io.new_id(),
        )
        with synced_lock(paths, purpose="api-ask"):
            log_io.insert_entry(paths.stream_md, answer_entry)
            sync.commit_safe(paths.log_dir, f"api: gemini answer ({anchor})")
    return JSONResponse({
        "is_question": result.is_question,
        "answer": result.answer,
        "error": result.error,
        "answer_entry": _entry_to_dict(answer_entry) if answer_entry else None,
    })


# --------------------------------------------------------------- promote --
@app.post("/api/promote")
def post_promote(req: PromoteRequest) -> JSONResponse:
    """Copy a stream-only entry into log.md (manual misclassification recovery).

    The entry's timestamp + source must match exactly. Idempotent on 409 —
    already-in-log means the auto-promotion already happened or the user
    clicked twice.
    """
    paths = config.load_paths()

    def _matches(e: log_io.Entry) -> bool:
        if req.id:
            return e.id == req.id
        if req.timestamp and req.source:
            return e.timestamp.isoformat(timespec="milliseconds") == req.timestamp and e.source == req.source
        return False

    if not req.id and not (req.timestamp and req.source):
        raise HTTPException(status_code=422, detail="provide id, or both timestamp and source")

    target: log_io.Entry | None = None
    for e in _read_entries(paths.stream_md):
        if _matches(e):
            target = e
            break
    if target is None:
        raise HTTPException(status_code=404, detail="entry not found in stream.md")
    for e in _read_entries(paths.log_md):
        if _matches(e):
            raise HTTPException(status_code=409, detail="already in log.md")
    with synced_lock(paths, purpose="api-promote"):
        log_io.insert_entry(paths.log_md, target)
        _render_safe(paths)
        sync.commit_safe(paths.log_dir, f"promote: {target.source}")
    return JSONResponse({**_entry_to_dict(target), "file": "log"})


# -------------------------------------------------------------------- tts --
@app.post("/api/tts")
def post_tts(req: TTSRequest) -> Response:
    voice = req.voice or tts.DEFAULT_VOICE
    # Log the full text we're synthesising so the container log shows exactly
    # what was spoken back. Useful when the client and server disagree on what
    # text reached TTS (encoding, truncation, stray chars). No truncation.
    log.info("tts request: voice=%s len=%d text=%r", voice, len(req.text), req.text)
    try:
        wav = tts.synthesize_wav(req.text, voice=voice)
    except Exception as e:
        log.exception("tts failed")
        raise HTTPException(status_code=500, detail=f"tts failed: {e}")
    return Response(content=wav, media_type="audio/wav")


# ------------------------------------------------------------- internals --
def _render_safe(paths: config.Paths) -> None:
    try:
        render_html.render(paths.log_md)
    except Exception:
        log.exception("auto-render failed; log.md fine, log.html may be stale")


def _mount_static() -> None:
    """Mount the frontend dir at /static. Tolerates missing dir at import time
    (uvicorn reloader, tests) so the module still loads."""
    frontend = _frontend_dir()
    if frontend.is_dir():
        app.mount("/static", StaticFiles(directory=frontend), name="static")
    else:
        log.warning("frontend dir not found at %s; /static will 404", frontend)


_mount_static()
