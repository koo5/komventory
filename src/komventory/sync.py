"""Git-backed sync layer on top of the cross-process lock.

`data/log` is a git working tree shared with a peer clone (e.g. `/home/koom/log`).
Before any operation that touches `log.md` we `git pull --ff-only` so we
don't write on top of stale state. On pull failure we behave like the lock
itself: log a warning, release the lock, sleep, retry.

If `data/log` is not a git repo, or has no remotes configured, pull is a
no-op — useful while you're still setting up, and for fresh checkouts that
haven't been wired to anything yet.
"""

from __future__ import annotations

import logging
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config
from .lock import komventory_lock
from .log_io import LOG_MD_READONLY_MODE

log = logging.getLogger(__name__)

# Exponential backoff on persistent pull failures so a long-running misconfig
# (e.g. an unresolved merge conflict) doesn't spam the log. Doubles until cap.
PULL_RETRY_BASE_S = 5.0
PULL_RETRY_MAX_S = 300.0  # 5 minutes


class GitPullFailed(Exception):
    pass


class GitCommitFailed(Exception):
    pass


def _is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def _has_remote(path: Path) -> bool:
    r = subprocess.run(
        ["git", "-C", str(path), "remote"],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and bool(r.stdout.strip())


def pull(repo_path: Path) -> None:
    """`git pull --no-rebase <remote> <branch>` in `repo_path`.

    Allows merge commits when host and peer have both moved on; chronological
    insert into log.md keeps non-conflicting edits in separate regions of the
    file most of the time, so a merge usually resolves cleanly. If the two
    sides edit the same line, the pull errors out and the lock-retry loop will
    keep complaining until you resolve the conflict by hand.

    Remote and branch are explicit (config.GIT_REMOTE / GIT_BRANCH, default
    `origin` / `main`) so this works without `branch.<x>.merge` upstream
    tracking set up. No-op if not a git repo or the remote isn't configured.
    """
    if not _is_git_repo(repo_path):
        log.debug("not a git repo, skipping pull: %s", repo_path)
        return
    if not _has_remote(repo_path):
        log.debug("git repo has no remotes, skipping pull: %s", repo_path)
        return
    r = subprocess.run(
        ["git", "-C", str(repo_path), "pull", "--no-rebase", config.GIT_REMOTE, config.GIT_BRANCH],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        msg = (r.stderr.strip() or r.stdout.strip() or "unknown error").splitlines()[-1]
        raise GitPullFailed(msg)


def commit(repo_path: Path, message: str) -> bool:
    """Stage only `log.md` and commit with `message`.

    By design we never version anything else in `data/log/` — media is huge and
    binary, `log.html` is a derived view, and `.idea/`/`.gitkeep` are noise.
    Returns True if a commit was created, False if there was nothing to commit
    or log.md doesn't exist yet or the dir isn't a git repo.
    """
    if not _is_git_repo(repo_path):
        log.debug("not a git repo, skipping commit: %s", repo_path)
        return False
    log_md = repo_path / "log.md"
    if not log_md.exists():
        log.debug("log.md not present yet, nothing to commit")
        return False
    r = subprocess.run(
        ["git", "-C", str(repo_path), "add", "--", "log.md"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise GitCommitFailed(f"git add log.md: {r.stderr.strip()}")
    # Anything staged? (--cached restricted to log.md to ignore unrelated index state.)
    r = subprocess.run(
        ["git", "-C", str(repo_path), "diff", "--cached", "--quiet", "--", "log.md"],
    )
    if r.returncode == 0:
        log.debug("no changes to log.md to commit")
        return False
    r = subprocess.run(
        ["git", "-C", str(repo_path), "commit", "-m", message, "--", "log.md"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise GitCommitFailed(r.stderr.strip() or r.stdout.strip())
    log.info("committed: %s", message.splitlines()[0])
    return True


def commit_safe(repo_path: Path, message: str) -> bool:
    """Like `commit()` but swallows GitCommitFailed with a loud error log.

    Why: a propagated commit failure leaves the working tree dirty. The next
    `pull --no-rebase` against a dirty tree fails ("your local changes would be
    overwritten"), and synced_lock enters a perpetual backoff loop the user
    won't notice. We'd rather report the failure clearly and keep going — the
    user fixes the git state by hand once.
    """
    try:
        return commit(repo_path, message)
    except GitCommitFailed as e:
        log.error(
            "git commit failed: %s\n"
            "  log.md is modified-and-uncommitted in %s. Next pull will fail until you fix it.\n"
            "  Recover with:  cd %s && git status   (resolve, then `git commit -- log.md`)",
            e, repo_path, repo_path,
        )
        return False


@contextmanager
def synced_lock(paths: config.Paths, purpose: str = "write") -> Iterator[None]:
    """Acquire the komventory lock + pull data/log; yield with both in hand.

    On pull failure, release the lock, sleep, retry. Exceptions raised by the
    yielded block propagate normally (we release the lock and re-raise).
    """
    backoff = PULL_RETRY_BASE_S
    while True:
        with komventory_lock(paths, purpose=purpose):
            try:
                pull(paths.log_dir)
            except GitPullFailed as e:
                log.warning("git pull failed: %s; retrying in %.0fs", e, backoff)
            else:
                # Re-assert the read-only invariant on log.md: git doesn't track
                # unix mode by default, so a pull may have landed it as 0644.
                log_md = paths.log_md
                if log_md.exists():
                    try:
                        import os as _os
                        _os.chmod(log_md, LOG_MD_READONLY_MODE)
                    except OSError:
                        pass
                yield
                return
        time.sleep(backoff)
        backoff = min(backoff * 2, PULL_RETRY_MAX_S)
