"""Git-backed sync layer on top of the cross-process lock.

`data/log` is a git working tree shared with a peer clone (e.g. `/home/you/log`).
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


def _ensure_git_repo(path: Path) -> bool:
    """`git init` the log dir on first use so entries are versioned out of the
    box — no manual setup. Idempotent (a no-op once `.git` exists). Returns
    False (skip versioning) only if the dir doesn't exist yet or init fails;
    host-to-host sync stays opt-in (add a remote to the repo afterwards).

    Default branch + `safe.directory` come from the mounted gitconfig; commit
    identity from the GIT_*_NAME/EMAIL env. See compose.yml / docker/gitconfig.
    """
    if _is_git_repo(path):
        return True
    if not path.is_dir():
        return False
    r = subprocess.run(
        ["git", "-C", str(path), "init", "-q"], capture_output=True, text=True
    )
    if r.returncode != 0:
        log.warning("git init failed for %s: %s", path, r.stderr.strip())
        return False
    log.info("initialised git repo for the log at %s", path)
    return True


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


VERSIONED_FILES = ("log.md", "stream.md")


def commit(repo_path: Path, message: str) -> bool:
    """Stage `log.md` + `stream.md` and commit with `message`.

    By design we only version those two — media is huge and binary, `log.html`
    is a derived view, and `.idea/`/`.gitkeep` are noise. Either or both files
    being dirty triggers a commit; if both are clean, returns False.
    """
    if not _ensure_git_repo(repo_path):
        log.debug("log dir not present yet, nothing to commit: %s", repo_path)
        return False
    present = [f for f in VERSIONED_FILES if (repo_path / f).exists()]
    if not present:
        log.debug("no versioned files present yet, nothing to commit")
        return False
    r = subprocess.run(
        ["git", "-C", str(repo_path), "add", "--", *present],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise GitCommitFailed(f"git add {present}: {r.stderr.strip()}")
    # Anything staged? Restrict the diff check to the files we stage so
    # unrelated index state doesn't accidentally trigger a commit.
    r = subprocess.run(
        ["git", "-C", str(repo_path), "diff", "--cached", "--quiet", "--", *present],
    )
    if r.returncode == 0:
        log.debug("no changes to %s to commit", present)
        return False
    r = subprocess.run(
        ["git", "-C", str(repo_path), "commit", "-m", message, "--", *present],
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
                # Re-assert the read-only invariant on log.md and stream.md:
                # git doesn't track unix mode by default, so a pull may have
                # landed them as 0644.
                import os as _os
                for p in (paths.log_md, paths.stream_md):
                    if p.exists():
                        try:
                            _os.chmod(p, LOG_MD_READONLY_MODE)
                        except OSError:
                            pass
                yield
                return
        time.sleep(backoff)
        backoff = min(backoff * 2, PULL_RETRY_MAX_S)
