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
        "timestamp": e.timestamp.isoformat(timespec="seconds"),
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


class TTSRequest(BaseModel):
    text: str = Field(min_length=1)
    voice: str | None = None


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
@app.get("/api/log/recent")
def log_recent(n: int = 50) -> JSONResponse:
    paths = config.load_paths()
    text = paths.log_md.read_text(encoding="utf-8") if paths.log_md.exists() else ""
    entries = list(log_io.iter_entries(text))
    return JSONResponse([_entry_to_dict(e) for e in entries[-n:]])


@app.get("/api/log/stream")
async def log_stream() -> StreamingResponse:
    """SSE: emit a `log-changed` event whenever log.md's mtime changes.

    Client receives the ping and refetches /api/log/recent — simple and robust
    against chronological inserts that can land anywhere in the file.
    """
    paths = config.load_paths()

    async def gen():
        last_mtime: float | None = None
        # Initial hello so the client knows the stream is open.
        yield "event: hello\ndata: {}\n\n"
        while True:
            await asyncio.sleep(0.5)
            try:
                mtime = paths.log_md.stat().st_mtime
            except FileNotFoundError:
                mtime = 0.0
            if last_mtime is None:
                last_mtime = mtime
                continue
            if mtime != last_mtime:
                last_mtime = mtime
                yield "event: log-changed\ndata: {}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Disable proxy buffering for SSE through Caddy/nginx.
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
        loc=note.where,
    )
    with synced_lock(paths, purpose="api-text"):
        log_io.insert_entry(paths.log_md, entry)
        _render_safe(paths)
        sync.commit_safe(paths.log_dir, "api: text note")
    return JSONResponse(_entry_to_dict(entry))


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
    with synced_lock(paths, purpose="api-audio"):
        try:
            entry = ingest.ingest_one(dest, paths)
        except Exception:
            log.exception("ingest_one failed for %s", dest)
            dest.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail="ingest failed")
        if entry is not None:
            _render_safe(paths)
            sync.commit_safe(paths.log_dir, f"api: audio note ({entry.source})")
    if entry is None:
        # 204 forbids a body; an empty Response is the only legal shape.
        return Response(status_code=204)
    return JSONResponse(_entry_to_dict(entry))


# -------------------------------------------------------------------- ask --
@app.post("/api/ask")
def post_ask(req: AskRequest) -> JSONResponse:
    paths = config.load_paths()
    log_text = paths.log_md.read_text(encoding="utf-8") if paths.log_md.exists() else ""
    result = qa.classify_and_answer(req.text, log_text)
    return JSONResponse({"is_question": result.is_question, "answer": result.answer})


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
