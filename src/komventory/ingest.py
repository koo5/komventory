"""Take a file from inbox, produce a log entry, route attachments under log/media/.

Inbox subdirs come in two flavours:
  - "owned" (openclaw, imports): we manage them; delete source after appending.
  - "read-only" (audio, video): bind-mounted from phone-sync dirs; we mustn't
    delete the source (it'd propagate back over Syncthing). We mark them in a
    ledger and skip on subsequent runs.

Subdir matching tolerates symlinks: each candidate inbox subdir is resolved and
the file's resolved path is checked against each. This means `data/inbox/audio`
can be a symlink to anywhere on the host without breaking subdir classification.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import config, frames, qa, render_html, sync, timestamps, transcribe
from .log_io import Entry, insert_entry, new_id
from .state import ProcessedLedger
from .sync import synced_lock

log = logging.getLogger(__name__)

READ_ONLY_SUBDIRS = {"audio", "video"}

_SYNCTHING_TMP = re.compile(r"^(\.syncthing|~syncthing~).*\.tmp$")
_ANDROID_TRASH = re.compile(r"^\.trashed-")
_IGNORE_NAMES = {".gitkeep", ".stignore", ".processed.json"}
_IGNORE_DIRS = {".stfolder", ".stversions", ".thumbnails"}


class UnsupportedFile(Exception):
    pass


def _should_ignore(path: Path) -> bool:
    name = path.name
    if name.startswith(".") or name in _IGNORE_NAMES:
        return True
    if _SYNCTHING_TMP.match(name) or _ANDROID_TRASH.match(name):
        return True
    return False


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


def _subdir_map(paths: config.Paths) -> dict[str, Path]:
    return {
        "audio": paths.inbox_audio,
        "video": paths.inbox_video,
        "openclaw": paths.inbox_openclaw,
        "imports": paths.inbox_imports,
        "pwa": paths.inbox_pwa,
    }


def _locate(path: Path, paths: config.Paths) -> tuple[str, str] | None:
    """Return (subdir_name, rel_under_subdir) or None if file isn't in any inbox subdir.

    Resolves symlinks on both sides so a symlinked inbox subdir still matches.
    """
    resolved = path.resolve()
    for name, root in _subdir_map(paths).items():
        if not root.exists():
            continue
        try:
            rel = resolved.relative_to(root.resolve())
        except ValueError:
            continue
        return name, str(rel)
    return None


def _stage_attachment(src: Path, paths: config.Paths, subdir: str, rel: str) -> Path:
    dest = paths.media / subdir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.resolve() != src.resolve():
        shutil.copy2(src, dest)
    return dest


def _rel_to_log(path: Path, paths: config.Paths) -> str:
    return str(path.resolve().relative_to(paths.log_dir.resolve()))


def _make_ledger(paths: config.Paths) -> ProcessedLedger:
    return ProcessedLedger(paths.inbox / ".processed.json")


def commit_entry(entry: Entry, paths: config.Paths, kind: str) -> None:
    """Write the entry to stream.md (always) and to log.md (conditionally).

    stream.md is the event-sourced river — every PWA capture, every ingested
    file. log.md is the curated inventory — auto-populated by promotion at
    write time, but hand-editable in the fork.

    Promotion rules:
      - image / note: always promote (factual; hand-curated note files were
        authored elsewhere with intent already).
      - audio / video / text: classify by `qa._looks_like_question`. Notes
        promote; questions stay stream-only. Misclassifications recover via
        the manual /api/promote endpoint.
    """
    insert_entry(paths.stream_md, entry)
    if kind in ("image", "note"):
        insert_entry(paths.log_md, entry)
        return
    if not qa._looks_like_question(entry.body):
        insert_entry(paths.log_md, entry)


@dataclass
class Prepared:
    """Output of the unlocked heavy phase, consumed by the locked commit phase.

    `entry=None` means the file yielded nothing committable (no speech / empty
    video) but still needs its bookkeeping: ledger-mark with `skip_tag` for
    read-only sources, source unlink for owned ones.
    """
    entry: Entry | None
    kind: str
    source_path: Path
    ledger_key: str
    is_read_only: bool
    force: bool = False
    skip_tag: str | None = None
    # Media artifacts staged during prepare. Removed only when the commit
    # phase *discards* the work (another ingester beat us to the file); on
    # failure they stay put so a retry re-stages over them.
    staged: list[Path] = field(default_factory=list)


def _discard_staged(prepared: Prepared) -> None:
    for p in prepared.staged:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


def prepare_one(
    file_path: Path,
    paths: config.Paths,
    *,
    force: bool = False,
    ledger: ProcessedLedger,
) -> Prepared | None:
    """Heavy phase: classify, stage, extract frames, transcribe. NO LOCK NEEDED.

    Only touches private artifacts (staged copies under media/, which git never
    versions). The ledger check here is optimistic — `commit_prepared` re-checks
    under the lock before mutating anything shared.
    """
    path = file_path
    if not path.is_file():
        log.warning("skipping non-file: %s", path)
        return None
    if _should_ignore(path):
        return None

    located = _locate(path, paths)
    if not located:
        log.warning("file is outside known inbox subdirs: %s", path)
        return None
    subdir, rel = located
    is_read_only = subdir in READ_ONLY_SUBDIRS
    ledger_key = f"{subdir}/{rel}"

    if is_read_only and not force and ledger.is_processed(ledger_key, path):
        log.debug("already processed: %s", ledger_key)
        return None

    kind = _classify(path)
    ts = timestamps.resolve(path)
    source_label = f"{subdir}/{rel}"

    def _prepared(entry: Entry | None, *, skip_tag: str | None = None, staged: list[Path] | None = None) -> Prepared:
        return Prepared(
            entry=entry, kind=kind, source_path=path, ledger_key=ledger_key,
            is_read_only=is_read_only, force=force, skip_tag=skip_tag,
            staged=staged or [],
        )

    if kind == "note":
        body = path.read_text(encoding="utf-8").strip()
        return _prepared(Entry(timestamp=ts, source=f"note@{source_label}", body=body, id=new_id()))

    if kind == "image":
        staged = _stage_attachment(path, paths, subdir, rel)
        entry = Entry(
            timestamp=ts,
            source=f"image@{source_label}",
            body=f"(image: {path.name})",
            id=new_id(),
            attachments=[_rel_to_log(staged, paths)],
        )
        return _prepared(entry, staged=[staged])

    if kind == "audio":
        staged = _stage_attachment(path, paths, subdir, rel)
        log.info("transcribing %s ...", source_label)
        text = (transcribe.transcribe(staged) or "").strip()
        if not text:
            # No speech detected — don't commit an empty entry. For read-only
            # subdirs we still mark it processed so we don't re-transcribe on
            # every sweep; the staged copy is cleaned up either way.
            log.info("no speech in %s — skipping", source_label)
            staged.unlink(missing_ok=True)
            return _prepared(None, skip_tag=f"skipped:no-speech@{source_label}")
        entry = Entry(
            timestamp=ts,
            source=f"whisper@{source_label}",
            body=text,
            id=new_id(),
            attachments=[_rel_to_log(staged, paths)],
        )
        return _prepared(entry, staged=[staged])

    if kind == "video":
        staged = _stage_attachment(path, paths, subdir, rel)
        frame_dir = paths.media / subdir / f"{rel}.frames"
        audio_tmp = paths.media / subdir / f"{rel}.audio.wav"
        log.info("extracting frames from %s ...", source_label)
        # Frames are nice-to-have; transcription is the actual content. Don't lose the
        # whole entry if ffmpeg trips on a quirky video (Android colorspace etc).
        try:
            frame_paths = frames.extract_frames(staged, frame_dir)
        except Exception as e:
            log.warning("frame extraction failed for %s (%s) — continuing with audio only", source_label, e)
            frame_paths = []
        log.info("extracting audio + transcribing %s ...", source_label)
        frames.extract_audio_track(staged, audio_tmp)
        text = (transcribe.transcribe(audio_tmp) or "").strip()
        audio_tmp.unlink(missing_ok=True)
        if not text and not frame_paths:
            # Nothing salvageable from this video — skip.
            log.info("no speech and no frames in %s — skipping", source_label)
            staged.unlink(missing_ok=True)
            return _prepared(None, skip_tag=f"skipped:empty@{source_label}")
        attachments = [_rel_to_log(staged, paths)] + [_rel_to_log(f, paths) for f in frame_paths]
        entry = Entry(
            timestamp=ts,
            source=f"whisper+frames@{source_label}",
            body=text or "(frames only)",
            id=new_id(),
            attachments=attachments,
        )
        return _prepared(entry, staged=[staged, frame_dir])

    raise UnsupportedFile(kind)


def commit_prepared(prepared: Prepared, paths: config.Paths, ledger: ProcessedLedger) -> Entry | None:
    """Mutation phase: insert entry, mark ledger / unlink source. CALLER HOLDS THE LOCK.

    Re-checks against fresh shared state: another ingester (ad-hoc host run vs
    the watcher) may have processed the same file while we were transcribing.
    In that case the staged artifacts are discarded and nothing is written.
    """
    ledger.reload()
    src = prepared.source_path

    if prepared.is_read_only:
        if not prepared.force and ledger.is_processed(prepared.ledger_key, src):
            log.info("lost ingest race for %s — discarding prepared entry", prepared.ledger_key)
            _discard_staged(prepared)
            return None
    elif not src.exists():
        # Owned source vanished mid-prepare: another ingester consumed it (or
        # the user deleted it). Either way the entry must not land twice.
        log.info("source gone before commit: %s — discarding prepared entry", prepared.ledger_key)
        _discard_staged(prepared)
        return None

    if prepared.entry is None:
        # Bookkeeping-only skip (no speech / empty video).
        if prepared.is_read_only:
            ledger.mark(prepared.ledger_key, src, prepared.skip_tag or "skipped")
        else:
            src.unlink(missing_ok=True)
        return None

    commit_entry(prepared.entry, paths, prepared.kind)
    if prepared.is_read_only:
        ledger.mark(prepared.ledger_key, src, prepared.entry.source)
    else:
        src.unlink(missing_ok=True)
    log.info("appended entry from %s", prepared.ledger_key)
    return prepared.entry


def _render_safe(paths: config.Paths) -> None:
    try:
        render_html.render(paths.log_md)
    except Exception:
        log.exception("auto-render failed; log.md is fine, log.html may be stale")


def ingest_one(
    file_path: Path,
    paths: config.Paths | None = None,
    *,
    force: bool = False,
    ledger: ProcessedLedger | None = None,
    commit_label: str = "ingest",
) -> Entry | None:
    """Ingest one file: heavy work unlocked, then a short lock for the mutation tail.

    Staging and transcription run without the komventory lock — they only touch
    private media/ artifacts — so PWA writes and other ingesters aren't blocked
    for the duration of a Whisper run. The lock is held just for: pull, insert,
    ledger/unlink, render, git commit.
    """
    paths = paths or config.load_paths()
    ledger = ledger if ledger is not None else _make_ledger(paths)

    prepared = prepare_one(file_path, paths, force=force, ledger=ledger)
    if prepared is None:
        return None

    with synced_lock(paths, purpose=f"ingest:{file_path.name}"):
        entry = commit_prepared(prepared, paths, ledger)
        if entry is not None:
            _render_safe(paths)
            sync.commit_safe(paths.log_dir, f"{commit_label}: {entry.source}")
    return entry


def _walk_subdir(root: Path):
    """Yield ingestable files under `root`, pruning ignored dirs/artifacts.

    Works through a symlink: if `root` itself is a symlink to a directory,
    Path.walk() walks the target. Nested symlinks are not followed by default,
    which is what we want (avoids following .stversions backlinks etc.).
    """
    if not root.exists():
        return
    for dirpath, dirnames, filenames in root.walk():
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS and not d.startswith(".")]
        for name in sorted(filenames):
            f = dirpath / name
            if f.is_file() and not _should_ignore(f):
                yield f


def rebuild_ledger(paths: config.Paths | None = None) -> int:
    """Reconstruct .processed.json from log.md.

    For every audio/video source file referenced by a `source:` tag in log.md,
    write a ledger entry with the current file's (size, mtime). Useful after a
    ledger was deleted, corrupted, or written with wrong ownership (e.g. by a
    Docker container running as root).
    """
    paths = paths or config.load_paths()
    ledger_path = paths.inbox / ".processed.json"
    # Directory is group-writable, so we can unlink even a file we don't own.
    if ledger_path.exists():
        ledger_path.unlink()
    ledger = ProcessedLedger(ledger_path)

    text = paths.log_md.read_text(encoding="utf-8") if paths.log_md.exists() else ""
    referenced = set(re.findall(r" source: (\S+)", text))

    count = 0
    for subdir_name, root in (("audio", paths.inbox_audio), ("video", paths.inbox_video)):
        if not root.exists():
            continue
        for f in _walk_subdir(root):
            try:
                rel = str(f.resolve().relative_to(root.resolve()))
            except ValueError:
                continue
            key = f"{subdir_name}/{rel}"
            tag = next((t for t in referenced if t.endswith(f"@{key}")), None)
            if tag is None:
                continue
            ledger.mark(key, f, tag)
            count += 1
    return count


def sweep_inbox(paths: config.Paths | None = None, *, force: bool = False) -> int:
    paths = paths or config.load_paths()
    ledger = _make_ledger(paths)
    count = 0
    for root in _subdir_map(paths).values():
        for f in _walk_subdir(root):
            try:
                if ingest_one(f, paths, force=force, ledger=ledger) is not None:
                    count += 1
            except UnsupportedFile as e:
                log.warning("skip: %s", e)
            except Exception:
                log.exception("ingest failed for %s", f)
    return count
