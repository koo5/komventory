"""Cross-process advisory lock for komventory writers.

Adapted from hillview/backend/tests/lock_util.py with two changes:

- The lock file carries a `host` field. PID-alive checks are only trusted when
  the holder's host matches ours, so a holder in a different PID namespace
  (e.g. the Docker watcher vs the host shell) never gets auto-claimed. If a
  foreign-host holder crashes you recover manually: `rm data/inbox/.lock`.
- API is a context manager.

Lock lives at `data/inbox/.lock`. The same path is visible to host and
container via the `./data` bind mount, so they can coordinate. The file is
JSON so a human inspecting `cat data/inbox/.lock` can see who's holding it.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from . import config

log = logging.getLogger(__name__)

POLL_S = 1.0


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but owned by another uid — still alive from our POV.
        return True
    except OSError:
        return False
    return True


def _read_holder(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _holder_payload(purpose: str) -> bytes:
    return (
        json.dumps(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "purpose": purpose,
                "acquired_at": datetime.now(tz=config.TIMEZONE).isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def _try_claim_stale(path: Path, stale_holder: dict, purpose: str) -> bool:
    """Atomically claim a stale lock by renaming aside, re-verifying, and replacing.

    The rename guards against the race where two processes both see the same
    stale lock and both try to claim it.
    """
    temp_path = path.with_name(path.name + f".claiming.{os.getpid()}")
    try:
        os.rename(path, temp_path)
    except OSError:
        return False

    actual = _read_holder(temp_path)
    if (
        not actual
        or actual.get("pid") != stale_holder.get("pid")
        or actual.get("host") != stale_holder.get("host")
    ):
        # Someone else swapped contents under us; back out.
        try:
            os.rename(temp_path, path)
        except OSError:
            temp_path.unlink(missing_ok=True)
        return False

    # Overwrite with our identity, then move into the canonical name.
    try:
        with temp_path.open("wb") as f:
            f.write(_holder_payload(purpose))
        os.rename(temp_path, path)
    except OSError:
        temp_path.unlink(missing_ok=True)
        return False

    log.warning(
        "claimed stale komventory lock from pid=%s host=%s",
        stale_holder.get("pid"),
        stale_holder.get("host"),
    )
    return True


@contextmanager
def komventory_lock(paths: config.Paths, purpose: str = "write") -> Iterator[None]:
    """Acquire a process-wide komventory write lock; release on context exit."""
    lock_path = paths.inbox / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    my_host = socket.gethostname()
    my_pid = os.getpid()
    warned = False

    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, _holder_payload(purpose))
            finally:
                os.close(fd)
            break
        except FileExistsError:
            pass

        holder = _read_holder(lock_path)
        if holder is None:
            # Corrupt or briefly-empty lock file; poll and retry.
            time.sleep(POLL_S)
            continue

        same_host = holder.get("host") == my_host
        pid = holder.get("pid")
        if same_host and isinstance(pid, int) and not _is_process_alive(pid):
            if _try_claim_stale(lock_path, holder, purpose):
                break
            # Lost the race; another process claimed first. Retry.
            continue

        if not warned:
            log.info(
                "waiting for komventory lock (held by pid=%s host=%s purpose=%s)%s",
                pid,
                holder.get("host"),
                holder.get("purpose"),
                "" if same_host else "; foreign host — will not auto-claim",
            )
            warned = True
        time.sleep(POLL_S)

    try:
        yield
    finally:
        # Release iff still ours.
        holder = _read_holder(lock_path)
        if (
            holder
            and holder.get("pid") == my_pid
            and holder.get("host") == my_host
        ):
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
