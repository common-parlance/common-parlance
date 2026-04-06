"""`dead-letters` command — inspect and retry failed uploads."""

from __future__ import annotations

import argparse
import json

from ._shared import resolve_db


def run(args: argparse.Namespace) -> None:
    from rich.console import Console
    from rich.table import Table

    from common_parlance.db import ConversationStore

    console = Console()
    db_path = resolve_db(args)

    with ConversationStore(db_path) as store:
        if args.retry:
            count = store.retry_dead_letters()
            if count:
                console.print(
                    f"[green]Reset {count} dead-lettered "
                    f"conversation(s) for retry.[/green]"
                )
            else:
                console.print("[dim]No dead-lettered conversations to retry.[/dim]")
            return

        rows = store.get_dead_letters()
        if not rows:
            console.print("[dim]No dead-lettered conversations.[/dim]")
            return

        table = Table(title=f"Dead-lettered conversations ({len(rows)})")
        table.add_column("ID", style="dim", max_width=12)
        table.add_column("Created", style="dim")
        table.add_column("Fails", justify="right")
        table.add_column("Turns", justify="right")
        table.add_column("Preview", max_width=60)

        for row in rows:
            turns = json.loads(row["scrubbed_turns"])
            first_content = turns[0]["content"] if turns else ""
            preview = first_content[:60] + ("..." if len(first_content) > 60 else "")
            table.add_row(
                row["id"][:12],
                row["created_at"][:19],
                str(row["fail_count"]),
                str(len(turns)),
                preview,
            )

        console.print(table)
        console.print("\n[dim]To retry: common-parlance dead-letters --retry[/dim]")
