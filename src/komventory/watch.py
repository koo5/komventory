"""Watchdog loop that calls ingest on new files in inbox/.

Recursive observation so nested subdirs in the phone-sync mounts (e.g.
`a22-recordings/Recording/`) are picked up without per-subdir scheduling.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import config, ingest

log = logging.getLogger(__name__)


class _IngestHandler(FileSystemEventHandler):
    def __init__(self, paths: config.Paths, ledger) -> None:
        self.paths = paths
        self.ledger = ledger

    def _maybe_ingest(self, src_path: str) -> None:
        p = Path(src_path)
        if not p.is_file() or ingest._should_ignore(p):
            return
        # Settle delay so partially-written Syncthing files don't get grabbed mid-write.
        # Syncthing renames .syncthing.<name>.tmp → <name> on completion, so a real
        # file landing here is usually already complete — 1s is a safety margin.
        time.sleep(1.0)
        if not p.exists():
            return
        try:
            # ingest_one transcribes WITHOUT the lock and takes it only for the
            # short mutation tail (insert/render/commit) — PWA writes are not
            # blocked for the duration of a Whisper run.
            ingest.ingest_one(p, self.paths, ledger=self.ledger)
        except ingest.UnsupportedFile as e:
            log.warning("skip: %s", e)
        except Exception:
            log.exception("ingest failed for %s", p)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_ingest(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_ingest(event.dest_path)


def run_forever() -> None:
    paths = config.load_paths()
    ledger = ingest._make_ledger(paths)
    # Sweep first so anything that arrived while the watcher was down gets
    # processed. Locking is per-file inside ingest_one, so a long backlog
    # doesn't hold the lock hostage and a restart loses at most one file's
    # commit window.
    n = ingest.sweep_inbox(paths)
    if n:
        log.info("initial sweep: %d entries", n)

    observer = Observer()
    handler = _IngestHandler(paths, ledger)
    for sub in (paths.inbox_audio, paths.inbox_video, paths.inbox_openclaw, paths.inbox_imports):
        if not sub.exists():
            continue
        observer.schedule(handler, str(sub), recursive=True)
    observer.start()
    log.info("watching %s", paths.inbox)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
