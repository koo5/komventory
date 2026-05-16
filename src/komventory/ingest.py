"""Take a file from inbox, produce a log entry, move attachments under log/media/."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import config, frames, transcribe
from .log_io import Entry, append_entry

log = logging.getLogger(__name__)


class UnsupportedFile(Exception):
    pass


def _classify(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in config.AUDIO_EXTS:
        return "audio"
    if ext in config.VIDEO_EXTS:
        return "video"
    if ext in config.IMAGE_EXTS:
        return "image"
    if ext in config.NOTE_EXTS:
        return "note"
    raise UnsupportedFile(f"no handler for {path.name}")


def _inbox_subdir(path: Path, paths: config.Paths) -> str:
    """Where in inbox/ did this file live? Used to bucket attachments under media/."""
    try:
        rel = path.resolve().relative_to(paths.inbox)
    except ValueError:
        return "misc"
    return rel.parts[0] if rel.parts else "misc"


def _stage_attachment(src: Path, paths: config.Paths, subdir: str) -> Path:
    """Copy src into log/media/<subdir>/ and return the destination path (relative to log_dir)."""
    dest = paths.media / subdir / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.resolve() != src.resolve():
        shutil.copy2(src, dest)
    return dest


def _rel_to_log(path: Path, paths: config.Paths) -> str:
    return str(path.resolve().relative_to(paths.log_dir.resolve()))


def ingest_one(file_path: Path, paths: config.Paths | None = None) -> Entry | None:
    """Process one inbox file, append an entry, and remove the source.

    Returns the appended Entry, or None if the file was skipped.
    """
    paths = paths or config.load_paths()
    path = file_path.resolve()
    if not path.is_file():
        log.warning("skipping non-file: %s", path)
        return None
    if path.name.startswith(".") or path.name == ".gitkeep":
        return None

    kind = _classify(path)
    subdir = _inbox_subdir(path, paths)
    now = datetime.now(timezone.utc).astimezone()

    if kind == "note":
        body = path.read_text(encoding="utf-8").strip()
        entry = Entry(timestamp=now, source=f"note@{subdir}/{path.name}", body=body)

    elif kind == "image":
        staged = _stage_attachment(path, paths, subdir)
        entry = Entry(
            timestamp=now,
            source=f"image@{subdir}/{path.name}",
            body=f"(image: {path.name})",
            attachments=[_rel_to_log(staged, paths)],
        )

    elif kind == "audio":
        staged = _stage_attachment(path, paths, subdir)
        log.info("transcribing %s ...", path.name)
        text = transcribe.transcribe(staged) or "(no speech detected)"
        entry = Entry(
            timestamp=now,
            source=f"whisper@{subdir}/{path.name}",
            body=text,
            attachments=[_rel_to_log(staged, paths)],
        )

    elif kind == "video":
        staged = _stage_attachment(path, paths, subdir)
        frame_dir = paths.media / subdir / f"{path.stem}.frames"
        audio_tmp = paths.media / subdir / f"{path.stem}.audio.wav"
        log.info("extracting frames from %s ...", path.name)
        frame_paths = frames.extract_frames(staged, frame_dir)
        log.info("extracting audio + transcribing %s ...", path.name)
        frames.extract_audio_track(staged, audio_tmp)
        text = transcribe.transcribe(audio_tmp) or "(no speech detected)"
        audio_tmp.unlink(missing_ok=True)
        attachments = [_rel_to_log(staged, paths)] + [_rel_to_log(f, paths) for f in frame_paths]
        entry = Entry(
            timestamp=now,
            source=f"whisper+frames@{subdir}/{path.name}",
            body=text,
            attachments=attachments,
        )

    else:
        raise UnsupportedFile(kind)

    append_entry(paths.log_md, entry)
    path.unlink()  # remove from inbox only after entry is persisted
    log.info("appended entry from %s", path.name)
    return entry


def sweep_inbox(paths: config.Paths | None = None) -> int:
    """Process every file currently in inbox/. Returns count of entries appended."""
    paths = paths or config.load_paths()
    count = 0
    for sub in (paths.inbox_audio, paths.inbox_video, paths.inbox_openclaw, paths.inbox_imports):
        if not sub.is_dir():
            continue
        for f in sorted(sub.iterdir()):
            if not f.is_file() or f.name.startswith(".") or f.name == ".gitkeep":
                continue
            try:
                if ingest_one(f, paths) is not None:
                    count += 1
            except UnsupportedFile as e:
                log.warning("skip: %s", e)
            except Exception:
                log.exception("ingest failed for %s", f)
    return count
