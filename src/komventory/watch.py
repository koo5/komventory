"""Watchdog loop that calls ingest on new files in inbox/."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import config, ingest

log = logging.getLogger(__name__)


class _IngestHandler(FileSystemEventHandler):
    def __init__(self, paths: config.Paths) -> None:
        self.paths = paths

    def _maybe_ingest(self, src_path: str) -> None:
        p = Path(src_path)
        if not p.is_file() or p.name.startswith(".") or p.name == ".gitkeep":
            return
        # Brief settle delay so partially-written Syncthing files don't get grabbed mid-write.
        time.sleep(1.0)
        if not p.exists():
            return
        try:
            ingest.ingest_one(p, self.paths)
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
    # Sweep first so anything that arrived while the watcher was down gets processed.
    n = ingest.sweep_inbox(paths)
    if n:
        log.info("initial sweep: %d entries", n)

    observer = Observer()
    handler = _IngestHandler(paths)
    for sub in (paths.inbox_audio, paths.inbox_video, paths.inbox_openclaw, paths.inbox_imports):
        sub.mkdir(parents=True, exist_ok=True)
        observer.schedule(handler, str(sub), recursive=False)
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
