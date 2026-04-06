"""`import` command — import conversations from files or directories."""

from __future__ import annotations

import argparse
import signal
import time
from pathlib import Path

from ._shared import resolve_db


def run(args: argparse.Namespace) -> None:
    from rich.console import Console

    from common_parlance.db import ConversationStore
    from common_parlance.importers import import_conversations

    console = Console()
    path = Path(args.path).expanduser().resolve()

    if not path.exists():
        console.print(f"[red]Not found: {path}[/red]")
        return

    db_path = resolve_db(args)

    def _do_import():
        if args.dry_run:
            return import_conversations(store=None, path=path, dry_run=True)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with ConversationStore(db_path) as store:
            return import_conversations(store=store, path=path)

    def _report(result):
        if result.imported:
            console.print(f"[green]Imported {result.imported} conversations.[/green]")
        if result.skipped_duplicate:
            console.print(
                f"[yellow]Skipped {result.skipped_duplicate} duplicates.[/yellow]"
            )
        if result.skipped_empty:
            console.print(f"[dim]Skipped {result.skipped_empty} empty/unusable.[/dim]")
        if result.skipped_malformed:
            console.print(
                f"[yellow]Skipped {result.skipped_malformed}"
                " malformed entries.[/yellow]"
            )
        for err in result.errors:
            console.print(f"[red]  {err}[/red]")
        if not result.imported and not result.errors:
            console.print("[dim]No conversations found to import.[/dim]")

    if args.daemon:
        if args.watch is None:
            console.print("[red]--daemon requires --watch[/red]")
            return
        from common_parlance.watcher import install_watcher

        plat, info = install_watcher(str(path), args.watch, db_path)
        console.print(f"[green]Watcher installed ({plat})[/green]")
        console.print(f"[dim]{info}[/dim]")
        return

    if args.watch is None:
        # Single import
        label = "Dry run" if args.dry_run else "Importing"
        console.print(f"[bold]{label}: {path.name}[/bold]")
        result = _do_import()
        _report(result)
        if result.imported and not args.dry_run:
            console.print(
                "\n[dim]Next: [bold]common-parlance process[/bold] "
                "— scrub PII before review[/dim]"
            )
        return

    # Watch mode: re-scan on interval. Dedup handles already-imported data.
    interval = max(1, args.watch) * 60
    stop = False

    def _handle_signal(sig, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    console.print(
        f"[bold]Watching {path.name} every {args.watch}m[/bold] "
        f"[dim](Ctrl+C to stop)[/dim]"
    )

    while not stop:
        result = _do_import()
        if result.imported:
            console.print(
                f"[green][{time.strftime('%H:%M')}] "
                f"Imported {result.imported} new conversations.[/green]"
            )
        # Sleep in short intervals so we can respond to signals
        deadline = time.monotonic() + interval
        while time.monotonic() < deadline and not stop:
            time.sleep(1)

    console.print("[dim]Watch stopped.[/dim]")
