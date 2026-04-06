"""`status` command — show system and pipeline status."""

from __future__ import annotations

import argparse
from pathlib import Path

from ._shared import resolve_db


def run(args: argparse.Namespace) -> None:
    from rich.console import Console
    from rich.table import Table

    from common_parlance.autostart import is_autostart_installed
    from common_parlance.config import load_config
    from common_parlance.consent import has_consent

    console = Console()
    config = load_config()
    db_path = resolve_db(args)

    # System status
    consented = has_consent()
    autostart = is_autostart_installed()
    has_key = bool(config.get("api_key"))

    console.print("[bold]System[/bold]")
    console.print(
        f"  Consent:    [{'green' if consented else 'dim'}]"
        f"{'granted' if consented else 'not granted'}[/]"
    )
    console.print(
        f"  Auto-start: [{'green' if autostart else 'dim'}]"
        f"{'enabled' if autostart else 'disabled'}[/]"
    )
    console.print(
        f"  API key:    [{'green' if has_key else 'yellow'}]"
        f"{'configured' if has_key else 'not set — run: common-parlance register'}[/]"
    )
    console.print(f"  Database:   [dim]{db_path}[/dim]")
    console.print()

    # Pipeline counts
    if not Path(db_path).exists():
        console.print(
            "[dim]No database yet. Import or capture some conversations first.[/dim]"
        )
        return

    from common_parlance.db import ConversationStore

    with ConversationStore(db_path) as store:
        stats = store.stats()

    table = Table(title="Pipeline")
    table.add_column("Stage", style="bold")
    table.add_column("Count", justify="right")

    table.add_row("Raw (unprocessed)", str(stats["raw"]))
    table.add_row("Pending review", str(stats["pending_review"]))
    table.add_row("Approved (ready)", str(stats["approved"]))
    table.add_row("Uploaded", str(stats["uploaded"]))
    if stats.get("dead_lettered"):
        table.add_row("Dead-lettered", str(stats["dead_lettered"]), style="red")
    if stats.get("no_ner"):
        table.add_row("No client NER", str(stats["no_ner"]), style="dim")

    console.print(table)

    # Suggest next action based on pipeline state
    if stats["raw"]:
        console.print(
            f"\n[dim]Next: [bold]common-parlance process[/bold] "
            f"— {stats['raw']} conversation(s) need PII scrubbing[/dim]"
        )
    elif stats["pending_review"]:
        console.print(
            f"\n[dim]Next: [bold]common-parlance review[/bold] "
            f"— {stats['pending_review']} conversation(s) pending review[/dim]"
        )
    elif stats["approved"]:
        console.print(
            f"\n[dim]Next: [bold]common-parlance upload[/bold] "
            f"— {stats['approved']} conversation(s) ready to upload[/dim]"
        )
