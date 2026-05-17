"""Komventory CLI."""

from __future__ import annotations

import logging
from pathlib import Path

import click

from . import config, ingest, import_gdoc as import_gdoc_mod, model_convert, render_html, sync, watch
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
    with synced_lock(paths, purpose="ingest"):
        if path is None:
            n = ingest.sweep_inbox(paths, force=force)
            if n:
                _render_safe(paths)
                sync.commit(paths.log_dir, f"ingest: sweep ({n} entries)")
            click.echo(f"appended {n} entries from inbox")
        else:
            result = ingest.ingest_one(path, paths, force=force)
            if result:
                _render_safe(paths)
                sync.commit(paths.log_dir, f"ingest: {result.source}")
            click.echo("appended 1 entry" if result else "skipped")


@main.command("watch")
def cmd_watch() -> None:
    """Watch inbox/ and ingest new files as they arrive."""
    watch.run_forever()


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
            sync.commit(paths.log_dir, f"import-gdoc: {path.name} ({n} entries)")
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
        "data", "log_dir", "log_md", "media",
        "inbox", "inbox_audio", "inbox_video", "inbox_openclaw", "inbox_imports",
        "cache_whisper",
    ):
        click.echo(f"{name:18s} {getattr(paths, name)}")


@main.command("lock-status")
def cmd_lock_status() -> None:
    """Show the current holder of the komventory lock, if any."""
    paths = config.load_paths()
    lock_path = paths.inbox / ".lock"
    if not lock_path.exists():
        click.echo("unlocked")
        return
    click.echo(lock_path.read_text(encoding="utf-8").strip())
