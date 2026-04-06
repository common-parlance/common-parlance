"""`consent` command — grant, revoke, or check consent status."""

from __future__ import annotations

import argparse
from pathlib import Path

from ._shared import resolve_db


def run(args: argparse.Namespace) -> None:
    from rich.console import Console

    from common_parlance.consent import (
        grant_consent,
        has_consent,
        revoke_consent,
    )

    console = Console()

    if args.grant:
        from rich.prompt import Prompt

        from common_parlance.consent import CONSENT_TEXT

        console.print()
        console.print(CONSENT_TEXT)
        console.print()

        answer = Prompt.ask(
            "[bold]Do you agree to contribute conversations?[/bold] [dim]\\[y/N][/dim]",
            default="n",
        )
        agreed = answer.strip().lower() in ("y", "yes")
        if not agreed:
            console.print("[dim]Consent not granted.[/dim]")
            return

        grant_consent()
        console.print(
            "[green]Consent granted. Conversations will be logged and uploaded.[/green]"
        )
        console.print(
            "\n[dim]Next: [bold]common-parlance import"
            " <file>[/bold] to import conversations\n"
            "  or: [bold]common-parlance proxy[/bold]"
            " to capture live conversations[/dim]"
        )
    elif args.revoke:
        revoke_consent()
        # Purge local data on consent revocation
        from common_parlance.db import ConversationStore

        db_path = resolve_db(args)
        if Path(db_path).exists():
            with ConversationStore(db_path) as store:
                counts = store.purge_all()
            console.print(
                f"[yellow]Purged {counts['exchanges']} raw exchanges "
                f"and {counts['staged']} staged conversations.[/yellow]"
            )
        console.print(
            "[yellow]Consent revoked. Future conversations will not be logged.[/yellow]"
        )
        console.print(
            "[dim]Note: already-uploaded anonymous data cannot be "
            "removed from the dataset.[/dim]"
        )
    else:
        # Default: show status
        if has_consent():
            console.print("[green]Consent: granted[/green]")
            console.print("[dim]Revoke with: common-parlance consent --revoke[/dim]")
        else:
            console.print("[dim]Consent: not granted[/dim]")
            console.print("[dim]Grant with: common-parlance consent --grant[/dim]")
