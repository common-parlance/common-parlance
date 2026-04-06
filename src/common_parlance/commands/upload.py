"""`upload` command — send approved conversations to the dataset."""

from __future__ import annotations

import argparse
from pathlib import Path

from ._shared import resolve_db


def run(args: argparse.Namespace) -> None:
    from rich.console import Console

    from common_parlance.config import load_config
    from common_parlance.db import ConversationStore
    from common_parlance.upload import upload_batch

    console = Console()
    config = load_config()
    db_path = resolve_db(args)

    if not Path(db_path).exists():
        console.print(
            "[dim]No database yet. Import or capture some conversations first.[/dim]"
        )
        return

    proxy_url = config["proxy_url"]
    api_key = config.get("api_key", "")

    # Catch missing API key early with actionable message
    if not api_key:
        console.print(
            "[red]No API key configured.[/red]\n"
            "[dim]Register with: common-parlance register[/dim]"
        )
        return

    with ConversationStore(db_path) as store:
        console.print(
            f"[bold]Uploading approved conversations via {proxy_url}...[/bold]"
        )

        count = upload_batch(
            store, proxy_url=proxy_url, api_key=api_key, limit=args.limit
        )
        if count:
            console.print(f"[green]Uploaded {count} conversations.[/green]")
            console.print(
                "\n[dim]Check pipeline: [bold]common-parlance status[/bold][/dim]"
            )
        else:
            console.print("[dim]No approved conversations to upload.[/dim]")
