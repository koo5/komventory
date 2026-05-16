"""Komventory CLI."""

from __future__ import annotations

import logging
from pathlib import Path

import click

from . import config, ingest, import_gdoc as import_gdoc_mod, watch


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@click.group()
@click.option("-v", "--verbose", is_flag=True)
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    _setup_logging(verbose)
    ctx.ensure_object(dict)


@main.command("ingest")
@click.argument("path", type=click.Path(path_type=Path, exists=True), required=False)
def cmd_ingest(path: Path | None) -> None:
    """Process one file, or sweep inbox/ if no path given."""
    paths = config.load_paths()
    if path is None:
        n = ingest.sweep_inbox(paths)
        click.echo(f"appended {n} entries from inbox")
    else:
        result = ingest.ingest_one(path, paths)
        click.echo("appended 1 entry" if result else "skipped")


@main.command("watch")
def cmd_watch() -> None:
    """Watch inbox/ and ingest new files as they arrive."""
    watch.run_forever()


@main.command("import-gdoc")
@click.argument("path", type=click.Path(path_type=Path, exists=True))
def cmd_import_gdoc(path: Path) -> None:
    """One-shot import of the existing Google Doc (exported as Markdown)."""
    paths = config.load_paths()
    n = import_gdoc_mod.import_gdoc(path, paths)
    click.echo(f"imported {n} entries from {path}")


@main.command("paths")
def cmd_paths() -> None:
    """Print resolved paths (for debugging mounts)."""
    paths = config.load_paths()
    for name in (
        "data", "log_dir", "log_md", "media",
        "inbox", "inbox_audio", "inbox_video", "inbox_openclaw", "inbox_imports",
        "cache_whisper",
    ):
        click.echo(f"{name:18s} {getattr(paths, name)}")
