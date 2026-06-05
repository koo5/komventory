"""Cross-process advisory lock for komventory writers.

Adapted from hillview/backend/tests/lock_util.py with two changes:

- The lock file carries a `host` field. PID-alive checks are only trusted when
  the holder's host matches ours, so a holder in a different PID namespace
  (e.g. the Docker watcher vs the host shell) never gets auto-claimed. If a
  foreign-host holder crashes you recover manually: `rm data/inbox/.lock`.
- API is a context manager.
- The lock file also carries `pid_start`, the holder's process start time from
  `/proc/<pid>/stat` (jiffies since kernel boot — shared across container
  restarts). A holder counts as alive only if a process with that PID exists
  *and* has the recorded start time. Without this, a restarted container gets
  stuck on its own ghost: the watcher is PID 1 every run and `hostname:` is
  pinned, so the pre-restart lock looks held by a live process forever.

Lock lives at `data/inbox/.lock`. The same path is visible to host and
container via the `./data` bind mount, so they can coordinate. The file is
JSON so a human inspecting `cat data/inbox/.lock` can see who's holding it.

Known limitation — multiple containers with the same `hostname:`
====================================================================
`compose.yml` pins `hostname: komventory-container` for visibility, which
means two concurrent `docker compose run --rm komventory ...` invocations
would both identify as the same host. PID-alive checks are then *unsafe*:
container A's PID 7 is in a different PID namespace from container B's PID 7,
so `os.kill(7, 0)` from B can report ESRCH for A's alive process → stale →
claim. Race.

The `pid_start` check narrows this considerably — B's PID 7 only impersonates
A's PID 7 if both processes also started within the same jiffy — but does not
eliminate it. In practice we don't run concurrent container instances (one
watcher + ad-hoc host commands is the model). If that pattern ever changes,
the right fix is to encode `/proc/self/ns/pid` (the namespace inode) into the
holder identity and only trust PID checks within the same namespace. Until
then, this is a documented hazard.
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
# An unparseable lock file older than this is treated as a dead creator
# (crashed between O_EXCL-create and payload write) and claimed. A healthy
# writer fills the file within milliseconds of creating it.
CORRUPT_STALE_S = 30.0


def _file_age_s(path: Path) -> float:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return 0.0


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


def _proc_start_time(pid: int) -> int | None:
    """Process start time in jiffies since kernel boot (field 22 of /proc/<pid>/stat).

    Together with the PID this uniquely identifies a process incarnation: a
    reused PID (e.g. the watcher being PID 1 in every container run) gets a
    different start time. Returns None if unreadable (process gone, non-Linux).
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        # comm (field 2) is in parens and may contain spaces/parens; split after the last ')'.
        fields = stat[stat.rindex(")") + 2 :].split()
        return int(fields[19])  # field 22, i.e. index 19 of the post-comm fields
    except (OSError, ValueError, IndexError):
        return None


def _is_holder_alive(holder: dict) -> bool:
    """True if the holder's process incarnation still exists on *this* host.

    Caller must have already established same-host. A bare PID-alive check is
    not enough: after a container restart the PID is typically reused by the
    new instance of the same program, so we also require the start time to
    match. Legacy locks without `pid_start` fall back to the PID-only check.
    """
    pid = holder.get("pid")
    if not isinstance(pid, int) or not _is_process_alive(pid):
        return False
    recorded_start = holder.get("pid_start")
    if recorded_start is None:
        return True  # legacy lock format — trust the PID check alone
    return _proc_start_time(pid) == recorded_start


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


def _holder_payload(
    purpose: str,
    session_id: str | None = None,
    no_auto_claim: bool = False,
) -> bytes:
    payload: dict = {
        "pid": os.getpid(),
        "pid_start": _proc_start_time(os.getpid()),
        "host": socket.gethostname(),
        "purpose": purpose,
        "acquired_at": datetime.now(tz=config.TIMEZONE).isoformat(timespec="seconds"),
    }
    if session_id is not None:
        # Stable identifier across hook invocations within a Claude Code session,
        # so hook-post can release a lock acquired by hook-pre (different PIDs).
        payload["session_id"] = session_id
    if no_auto_claim:
        # Don't let the PID-alive heuristic auto-claim this lock as stale; the
        # PID in the file is the (short-lived) hook process, not the holder.
        payload["no_auto_claim"] = True
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _try_claim_stale(
    path: Path,
    stale_holder: dict | None,
    purpose: str,
    session_id: str | None = None,
    no_auto_claim: bool = False,
) -> bool:
    """Atomically claim a stale lock by renaming aside, re-verifying, and replacing.

    The rename guards against the race where two processes both see the same
    stale lock and both try to claim it. `stale_holder=None` means we are
    claiming a corrupt/empty lock file — verification then requires the
    renamed file to *still* be unparseable.
    """
    temp_path = path.with_name(path.name + f".claiming.{os.getpid()}")
    try:
        os.rename(path, temp_path)
    except OSError:
        return False

    actual = _read_holder(temp_path)
    if stale_holder is None:
        matches = actual is None
    else:
        matches = (
            actual is not None
            and actual.get("pid") == stale_holder.get("pid")
            and actual.get("pid_start") == stale_holder.get("pid_start")
            and actual.get("host") == stale_holder.get("host")
        )
    if not matches:
        # Someone else swapped contents under us; back out.
        try:
            os.rename(temp_path, path)
        except OSError:
            temp_path.unlink(missing_ok=True)
        return False

    # Overwrite with our identity, then move into the canonical name.
    try:
        with temp_path.open("wb") as f:
            f.write(_holder_payload(purpose, session_id=session_id, no_auto_claim=no_auto_claim))
        os.rename(temp_path, path)
    except OSError:
        temp_path.unlink(missing_ok=True)
        return False

    if stale_holder is None:
        log.warning("claimed corrupt/empty komventory lock file")
    else:
        log.warning(
            "claimed stale komventory lock from pid=%s host=%s",
            stale_holder.get("pid"),
            stale_holder.get("host"),
        )
    return True


def _try_create_lock_file(
    lock_path: Path,
    purpose: str,
    session_id: str | None = None,
    no_auto_claim: bool = False,
) -> bool:
    """Atomic `O_CREAT|O_EXCL` create + payload write. Returns True on success."""
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    try:
        os.write(fd, _holder_payload(purpose, session_id=session_id, no_auto_claim=no_auto_claim))
    finally:
        os.close(fd)
    return True


def _wait_and_acquire(
    paths: config.Paths,
    purpose: str,
    session_id: str | None = None,
    no_auto_claim: bool = False,
) -> None:
    """Blocking loop: create the lock file or wait/claim a stale one."""
    lock_path = paths.inbox / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    my_host = socket.gethostname()
    warned = False

    while True:
        if _try_create_lock_file(lock_path, purpose, session_id=session_id, no_auto_claim=no_auto_claim):
            return

        holder = _read_holder(lock_path)
        if holder is None:
            # Corrupt or empty lock file. Briefly-empty is normal (another
            # process sits between O_EXCL-create and payload write); one
            # staying empty past CORRUPT_STALE_S means its creator died
            # mid-write (e.g. container killed) — claim it by age.
            if _file_age_s(lock_path) > CORRUPT_STALE_S and _try_claim_stale(
                lock_path, None, purpose, session_id=session_id, no_auto_claim=no_auto_claim
            ):
                return
            time.sleep(POLL_S)
            continue

        same_host = holder.get("host") == my_host
        pid = holder.get("pid")
        if holder.get("no_auto_claim"):
            # Holder explicitly opted out of stale recovery (typically a hook
            # session). Wait until they release; manual `rm .lock` to break.
            pass
        elif same_host and not _is_holder_alive(holder):
            if _try_claim_stale(
                lock_path, holder, purpose, session_id=session_id, no_auto_claim=no_auto_claim
            ):
                return
            # Lost the race; another process claimed first. Retry.
            continue

        if not warned:
            log.info(
                "waiting for komventory lock (held by pid=%s host=%s purpose=%s session=%s)%s",
                pid,
                holder.get("host"),
                holder.get("purpose"),
                holder.get("session_id"),
                "" if same_host else "; foreign host — will not auto-claim",
            )
            warned = True
        time.sleep(POLL_S)


def acquire(
    paths: config.Paths,
    purpose: str = "write",
    session_id: str | None = None,
    no_auto_claim: bool = False,
) -> None:
    """Blocking, non-context-manager acquire. For shell verbs / Claude Code hooks.

    Caller is responsible for calling `release(paths)` later. The lock file
    persists on disk and is independent of this Python process — no generator
    cleanup deletes it on exit.

    `session_id` provides a stable identifier across processes (e.g., Claude
    Code hook-pre vs hook-post run in different PIDs but share a session). When
    set, release with the same session_id releases regardless of PID.
    `no_auto_claim` disables PID-alive stale-recovery for this lock — required
    for hook locks since the acquiring process exits immediately.
    """
    _wait_and_acquire(paths, purpose, session_id=session_id, no_auto_claim=no_auto_claim)


def release(paths: config.Paths, session_id: str | None = None) -> bool:
    """Release the lock. Returns True if we owned and released it, False otherwise.

    Ownership: if `session_id` is provided, release iff the lock's session_id
    matches. Otherwise fall back to PID+host match (for the in-process
    context-manager case).
    """
    lock_path = paths.inbox / ".lock"
    holder = _read_holder(lock_path)
    if not holder:
        return False
    if session_id is not None:
        owned = holder.get("session_id") == session_id
    else:
        owned = (
            holder.get("pid") == os.getpid()
            and holder.get("host") == socket.gethostname()
        )
    if not owned:
        return False
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    return True


@contextmanager
def komventory_lock(paths: config.Paths, purpose: str = "write") -> Iterator[None]:
    """Acquire a process-wide komventory write lock; release on context exit."""
    _wait_and_acquire(paths, purpose)
    lock_path = paths.inbox / ".lock"
    my_pid = os.getpid()
    my_host = socket.gethostname()
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
