"""`audit` command — scan staged conversations for PII leaks and over-redaction."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ._shared import resolve_db

if TYPE_CHECKING:
    from rich.console import Console

    from common_parlance.audit import AuditResult


def _print_audit(
    result: AuditResult,
    console: Console,
    brief: bool = False,
) -> None:
    """Print audit results.

    Two modes:
    - brief: one-line verdict + hint (used after process)
    - full: verdict + flagged conversations with details (audit command)
    """
    from rich.table import Table

    # --- Verdict lines (always shown) ---
    if result.has_leaks:
        console.print(
            f"  [red bold]FAIL[/red bold]  Leaks: {result.leak_count} "
            f"conversation(s) with potential PII"
        )
    else:
        console.print("  [green]PASS[/green]  Leaks: none detected")

    if result.high_density_count:
        console.print(
            f"  [yellow]WARN[/yellow]  {result.high_density_count} "
            f"conversation(s) heavily redacted (>25%)"
        )

    if brief:
        if result.has_leaks or result.high_density_count:
            console.print(
                "\n[dim]Run [bold]common-parlance audit[/bold] for details[/dim]"
            )
        return

    # --- Leak details (only if leaks found) ---
    leaked = [c for c in result.conversations if c.leaks]
    if leaked:
        console.print()
        table = Table(title="PII Leaks", title_style="red bold", border_style="red")
        table.add_column("ID", style="dim")
        table.add_column("Type")
        table.add_column("Examples")
        table.add_column("Preview")
        for c in leaked:
            for pii_type, examples in c.leaks.items():
                table.add_row(
                    c.conv_id,
                    pii_type,
                    ", ".join(examples),
                    c.preview[:40] + "...",
                )
        console.print(table)

    # --- Over-redaction details (only if flagged conversations exist) ---
    flagged = [c for c in result.conversations if c.density > 0.25]
    if flagged:
        console.print()
        table = Table(
            title="Heavily Redacted", title_style="yellow", border_style="yellow"
        )
        table.add_column("ID", style="dim")
        table.add_column("Turns", justify="right")
        table.add_column("Density", justify="right")
        table.add_column("Preview")
        for c in sorted(flagged, key=lambda c: c.density, reverse=True):
            table.add_row(
                c.conv_id,
                str(c.turn_count),
                f"{c.density:.0%}",
                c.preview[:50] + "...",
            )
        console.print(table)

    # --- Clean result ---
    if not result.has_leaks and not result.high_density_count:
        console.print(
            f"\n[dim]{result.total} conversations scanned, nothing to flag.[/dim]"
        )


def run(args: argparse.Namespace) -> None:
    """Quick PII leakage scan and over-redaction stats on staged conversations."""
    from rich.console import Console

    from common_parlance.audit import audit_conversations
    from common_parlance.db import ConversationStore

    console = Console()
    db_path = resolve_db(args)
    if not Path(db_path).exists():
        console.print("[dim]No database yet.[/dim]")
        return

    with ConversationStore(db_path) as store:
        if args.all:
            rows = store.conn.execute(
                "SELECT id, scrubbed_turns FROM staged ORDER BY created_at"
            ).fetchall()
            scope = "all staged"
        else:
            # Default: scan everything not yet uploaded (pending + approved)
            rows = store.conn.execute(
                "SELECT id, scrubbed_turns FROM staged "
                "WHERE uploaded = 0 ORDER BY created_at"
            ).fetchall()
            scope = "pending"
            # Fall back to uploaded if nothing pending
            if not rows:
                rows = store.conn.execute(
                    "SELECT id, scrubbed_turns FROM staged "
                    "WHERE uploaded = 1 ORDER BY created_at"
                ).fetchall()
                scope = "uploaded"

    if not rows:
        console.print("[dim]No conversations to audit.[/dim]")
        return

    console.print(f"[bold]Auditing {len(rows)} {scope} conversations...[/bold]\n")
    result = audit_conversations(rows)
    _print_audit(result, console, brief=args.brief)

    # Exit code 1 on leaks (for CI integration)
    if result.has_leaks:
        sys.exit(1)
