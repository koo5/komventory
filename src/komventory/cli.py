"""Komventory CLI."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import click

from . import config, ingest, import_gdoc as import_gdoc_mod, lock, model_convert, render_html, sync, watch
from .sync import synced_lock


def _render_safe(paths: config.Paths) -> None:
    """Render log.html after inserts. Non-fatal if it fails — the ingest still stands."""
    try:
        render_html.render(paths.log_md)
    except Exception:
        logging.getLogger(__name__).exception("auto-render failed; log.md is fine, log.html may be stale")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Third-party libraries spam DEBUG (httpx/httpcore log every header). Keep them quiet
    # even under -v; only komventory's loggers should respond to the verbose flag.
    for noisy in ("httpx", "httpcore", "urllib3", "filelock", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@click.group()
@click.option("-v", "--verbose", is_flag=True)
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    _setup_logging(verbose)
    ctx.ensure_object(dict)


@main.command("ingest")
@click.argument("path", type=click.Path(path_type=Path, exists=True), required=False)
@click.option("--force", is_flag=True, help="Re-ingest even if the file is in the processed ledger.")
def cmd_ingest(path: Path | None, force: bool) -> None:
    """Process one file, or sweep inbox/ if no path given."""
    paths = config.load_paths()
    # ingest_one locks per file around just the mutation tail (insert/render/
    # commit); transcription runs unlocked. Each entry gets its own git commit.
    if path is None:
        n = ingest.sweep_inbox(paths, force=force)
        click.echo(f"appended {n} entries from inbox")
    else:
        result = ingest.ingest_one(path, paths, force=force)
        click.echo("appended 1 entry" if result else "skipped")


@main.command("watch")
def cmd_watch() -> None:
    """Watch inbox/ and ingest new files as they arrive."""
    watch.run_forever()


@main.command("serve")
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", type=int,
              default=lambda: int(os.environ.get("KOMVENTORY_API_PORT", "3411")),
              show_default="$KOMVENTORY_API_PORT or 3411")
def cmd_serve(host: str, port: int) -> None:
    """Run the FastAPI PWA backend (note capture, browse, ask, TTS)."""
    import uvicorn  # local import: keep startup cheap for non-serve verbs
    uvicorn.run("komventory.api:app", host=host, port=port, log_level="info")


@main.command("render")
@click.option("-o", "--output", type=click.Path(path_type=Path),
              help="Output path; defaults to log.md's sibling log.html.")
def cmd_render(output: Path | None) -> None:
    """Render log.md into a browsable log.html (with inline media)."""
    paths = config.load_paths()
    with synced_lock(paths, purpose="render"):
        out = render_html.render(paths.log_md, output)
    click.echo(f"wrote {out}")


@main.command("rebuild-ledger")
def cmd_rebuild_ledger() -> None:
    """Reconstruct data/inbox/.processed.json from source: tags in log.md."""
    paths = config.load_paths()
    with synced_lock(paths, purpose="rebuild-ledger"):
        n = ingest.rebuild_ledger(paths)
    click.echo(f"ledger has {n} entries")


@main.command("import-gdoc")
@click.argument("path", type=click.Path(path_type=Path, exists=True))
def cmd_import_gdoc(path: Path) -> None:
    """One-shot import of the existing Google Doc (exported as Markdown)."""
    paths = config.load_paths()
    with synced_lock(paths, purpose="import-gdoc"):
        n = import_gdoc_mod.import_gdoc(path, paths)
        if n:
            _render_safe(paths)
            sync.commit_safe(paths.log_dir, f"import-gdoc: {path.name} ({n} entries)")
    click.echo(f"imported {n} entries from {path}")


@main.command("convert-model")
@click.argument("hf_name")
def cmd_convert_model(hf_name: str) -> None:
    """One-time CT2 conversion of an HF Whisper finetune (host-only; needs `--extra convert`)."""
    paths = config.load_paths()
    out = model_convert.convert(hf_name, paths.cache_whisper / "ct2")
    click.echo(f"converted: {out}")
    click.echo(f"set KOMVENTORY_WHISPER_MODEL={hf_name} (already the default in compose.yml if updated)")


@main.command("paths")
def cmd_paths() -> None:
    """Print resolved paths (for debugging mounts). Read-only — not locked."""
    paths = config.load_paths()
    for name in (
        "data", "log_dir", "log_md", "stream_md", "media",
        "inbox", "inbox_audio", "inbox_video", "inbox_openclaw", "inbox_imports", "inbox_pwa",
        "cache_whisper", "cache_piper",
    ):
        click.echo(f"{name:18s} {getattr(paths, name)}")


def _read_hook_stdin() -> tuple[str | None, str | None]:
    """Parse Claude Code's hook stdin JSON. Returns (file_path, session_id) or (None, None)."""
    import json as _json
    import sys as _sys
    try:
        data = _json.loads(_sys.stdin.read() or "{}")
    except _json.JSONDecodeError:
        return None, None
    file_path = (data.get("tool_input") or {}).get("file_path")
    session_id = data.get("session_id")
    return file_path, session_id


def _is_log_md(file_path: str | None, paths: config.Paths) -> bool:
    if not file_path:
        return False
    try:
        return Path(file_path).resolve() == paths.log_md.resolve()
    except (FileNotFoundError, OSError):
        return Path(file_path).name == paths.log_md.name


@main.command("hook-pre-edit")
def cmd_hook_pre_edit() -> None:
    """PreToolUse hook for log.md: acquire lock, pull, chmod +w.

    Reads Claude Code's hook JSON from stdin (file_path and session_id). No-op
    for any path other than data/log/log.md. The lock uses the Claude session
    id so hook-post-edit can release it regardless of process identity.
    """
    import os as _os
    file_path, session_id = _read_hook_stdin()
    paths = config.load_paths()
    if not _is_log_md(file_path, paths):
        return
    lock.acquire(
        paths,
        purpose=f"claude-edit:{Path(file_path).name}",
        session_id=session_id,
        no_auto_claim=True,
    )
    try:
        sync.pull(paths.log_dir)
    except sync.GitPullFailed as e:
        lock.release(paths, session_id=session_id)
        click.echo(f"git pull failed: {e} — resolve and retry the edit", err=True)
        raise SystemExit(1)
    if paths.log_md.exists():
        _os.chmod(paths.log_md, 0o644)
    click.echo(f"unlocked {paths.log_md.name} for editing (session={session_id})")


@main.command("hook-post-edit")
def cmd_hook_post_edit() -> None:
    """PostToolUse hook for log.md: render, chmod 444, commit, release lock.

    Always runs full cleanup even if the Edit failed — leaves invariants in
    the correct state regardless of what happened during the tool call.
    """
    import os as _os
    file_path, session_id = _read_hook_stdin()
    paths = config.load_paths()
    if not _is_log_md(file_path, paths):
        return
    try:
        _render_safe(paths)
        if paths.log_md.exists():
            _os.chmod(paths.log_md, 0o444)
        sync.commit_safe(paths.log_dir, "claude edit")
    finally:
        lock.release(paths, session_id=session_id)
    click.echo(f"re-locked {paths.log_md.name} and committed (session={session_id})")


@main.command("acquire-lock")
@click.option("--purpose", default="manual", help="Tag stored in the lock file for visibility.")
def cmd_acquire_lock(purpose: str) -> None:
    """Acquire the komventory lock. Blocks until granted. Caller must release-lock later.

    Note: the lock survives the lifetime of this command — same file-based
    primitive as the in-process context manager. Useful for hook scripts that
    need to bracket an external operation."""
    paths = config.load_paths()
    lock.acquire(paths, purpose=purpose)
    click.echo(f"acquired (purpose={purpose})")


@main.command("release-lock")
def cmd_release_lock() -> None:
    """Release the komventory lock iff held by this process+host."""
    paths = config.load_paths()
    lock.release(paths)
    click.echo("released")


@main.command("lock-status")
def cmd_lock_status() -> None:
    """Show the current holder of the komventory lock, if any."""
    paths = config.load_paths()
    lock_path = paths.inbox / ".lock"
    if not lock_path.exists():
        click.echo("unlocked")
        return
    click.echo(lock_path.read_text(encoding="utf-8").strip())
