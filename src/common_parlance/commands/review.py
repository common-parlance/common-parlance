"""`review` command — interactive approval/rejection/editing of conversations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ._shared import resolve_db


def _edit_turns(turns: list[dict], console) -> list[dict] | None:
    """Interactive manual redaction of conversation turns.

    Shows turns with numbered text snippets. User selects text to replace
    with [REDACTED]. Only allows adding redactions, never removing existing
    placeholders. Returns modified turns or None if cancelled.
    """
    import copy
    import re

    from rich.table import Table

    # Show turns with indices for selection
    console.print("\n[bold]Select a turn to edit:[/bold]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("#", style="dim", width=3)
    table.add_column("Role", width=10)
    table.add_column("Preview", no_wrap=False)
    for idx, turn in enumerate(turns):
        preview = turn["content"][:120]
        if len(turn["content"]) > 120:
            preview += "..."
        table.add_row(str(idx + 1), turn["role"], preview)
    console.print(table)

    turn_input = console.input("\n[dim]Turn number (or 'c' to cancel): [/dim]").strip()
    if turn_input.lower() in ("c", "cancel", ""):
        return None
    try:
        turn_idx = int(turn_input) - 1
        if turn_idx < 0 or turn_idx >= len(turns):
            console.print("[red]Invalid turn number.[/red]")
            return None
    except ValueError:
        console.print("[red]Invalid input.[/red]")
        return None

    turn_content = turns[turn_idx]["content"]

    # Show the full turn content with line numbers for context
    console.print(f"\n[bold]{turns[turn_idx]['role']}:[/bold]")
    lines = turn_content.split("\n")
    for line_idx, line in enumerate(lines):
        console.print(f"  [dim]{line_idx + 1:3}[/dim] {line}")

    console.print(
        "\n[dim]Enter text to redact (exact match, case-sensitive).[/dim]"
        "\n[dim]The matched text will be replaced with [REDACTED].[/dim]"
    )
    search_text = console.input("\n[dim]Text to redact (or 'c' to cancel): [/dim]")
    if search_text.lower() in ("c", "cancel") or not search_text:
        return None

    if search_text not in turn_content:
        console.print("[red]Text not found in this turn.[/red]")
        return None

    # Don't allow removing existing placeholders
    placeholder_pattern = re.compile(
        r"\[(?:NAME|EMAIL|PHONE|LOCATION|DATE|ADDRESS|GROUP|SSN|IP|SECRET|PATH"
        r"|CREDIT_CARD|URL|IBAN|MEDICAL_ID|DRIVER_LICENSE|REDACTED)"
        r"(?:_\d+)?(?::[^\]]+)?\]|<ORGANIZATION>"
    )
    if placeholder_pattern.fullmatch(search_text.strip()):
        console.print("[red]Cannot redact existing placeholders.[/red]")
        return None

    # Count occurrences
    count = turn_content.count(search_text)
    if count > 1:
        console.print(
            f"[yellow]Found {count} occurrences — all will be redacted.[/yellow]"
        )

    # Apply redaction
    new_turns = copy.deepcopy(turns)
    new_turns[turn_idx]["content"] = turn_content.replace(search_text, "[REDACTED]")

    # Re-run content filter on edited text
    from common_parlance.filter import KeywordContentFilter

    content_filter = KeywordContentFilter()
    edited_text = " ".join(t["content"] for t in new_turns)
    blocked = content_filter.check(edited_text)
    if blocked:
        console.print(
            f"[red]Content filter triggered after edit: {blocked}. Edit rejected.[/red]"
        )
        return None

    console.print(f"[green]Redacted {count} occurrence(s).[/green]")
    return new_turns


def run(args: argparse.Namespace) -> None:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    from common_parlance.db import ConversationStore

    console = Console()
    db_path = resolve_db(args)
    if not Path(db_path).exists():
        console.print(
            "[dim]No database yet. Import or capture some conversations first.[/dim]"
        )
        return
    with ConversationStore(db_path) as store:
        if args.reset:
            count = store.unapprove_all()
            if count:
                console.print(
                    f"[yellow]Reset {count} conversation(s)"
                    " back to pending review.[/yellow]"
                )
            else:
                console.print("[dim]No approved conversations to reset.[/dim]")
            return

        pending = store.get_pending_review()

        if not pending:
            # Hint toward upload if conversations are approved
            stats = store.stats()
            approved_count = stats["approved"]
            if approved_count:
                console.print(
                    "[dim]No conversations pending review.[/dim]\n"
                    f"[dim]Next: [bold]common-parlance upload[/bold] "
                    f"— {approved_count} conversation(s) ready to upload[/dim]"
                )
            else:
                console.print("[dim]No conversations pending review.[/dim]")
            return

        if args.approve_all:
            store.approve_batch([row["id"] for row in pending])
            console.print(f"[green]Approved {len(pending)} conversations.[/green]")
            stats = store.stats()
            approved_count = stats["approved"]
            if approved_count:
                console.print(
                    f"\n[dim]Next: [bold]common-parlance upload[/bold] "
                    f"— {approved_count} conversation(s) ready to upload[/dim]"
                )
            return

        console.print(
            f"[bold]{len(pending)} conversations pending review.[/bold]\n"
            "[dim]Commands: (a)pprove, (r)eject, (e)dit, (s)kip, (q)uit[/dim]\n"
        )

        approved = 0
        rejected = 0
        for i, row in enumerate(pending):
            turns = json.loads(row["scrubbed_turns"])
            edited = False

            while True:  # Re-display loop (for edit → re-show)
                content = Text()
                for turn in turns:
                    role = turn["role"]
                    style = "bold cyan" if role == "user" else "bold green"
                    content.append(f"{role}: ", style=style)
                    content.append(f"{turn['content']}\n\n")

                turn_count = len(turns)
                # Placeholder density warning
                from common_parlance.audit import _PLACEHOLDER_RE

                full_text = " ".join(t["content"] for t in turns)
                word_count = len(full_text.split())
                placeholder_count = len(_PLACEHOLDER_RE.findall(full_text))
                density = placeholder_count / max(word_count, 1)
                density_warning = ""
                if density >= 0.25:
                    density_warning = (
                        f" · [yellow bold]⚠ {density:.0%} redacted[/yellow bold]"
                    )
                edited_tag = " · [magenta]edited[/magenta]" if edited else ""

                title = (
                    f"[{i + 1}/{len(pending)}]"
                    f" {row['id'][:8]}"
                    f" · {turn_count} turns"
                    f"{density_warning}{edited_tag}"
                    " · scroll: ↑↓ space b"
                    " · exit pager: q"
                )
                subtitle = "[dim]scroll: ↑↓ space b · press q to review[/dim]"
                panel = Panel(
                    content,
                    title=title,
                    subtitle=subtitle,
                    border_style="yellow" if density >= 0.25 else "blue",
                )

                # Use pager for long conversations (> terminal height)
                total_lines = sum(2 + turn["content"].count("\n") for turn in turns)
                use_pager = console.is_terminal and total_lines > console.height - 5
                if use_pager:
                    with console.pager(styles=True):
                        console.print(panel)
                else:
                    console.print(panel)

                choice = console.input("[a/r/e/s/q] > ").strip().lower()
                if choice in ("a", "approve"):
                    if edited:
                        store.update_scrubbed_turns(row["id"], json.dumps(turns))
                    store.approve(row["id"])
                    approved += 1
                    console.print("[green]Approved.[/green]")
                    break
                elif choice in ("r", "reject"):
                    store.reject(row["id"])
                    rejected += 1
                    console.print("[red]Rejected.[/red]")
                    break
                elif choice in ("e", "edit"):
                    result = _edit_turns(turns, console)
                    if result is not None:
                        turns = result
                        edited = True
                        console.print(
                            "[magenta]Redaction applied."
                            " Showing updated"
                            " conversation.[/magenta]\n"
                        )
                        # Re-display by continuing the while loop
                    else:
                        console.print("[dim]No changes made.[/dim]\n")
                    continue  # Re-show conversation
                elif choice in ("s", "skip"):
                    if edited:
                        # Save edits even if skipping
                        store.update_scrubbed_turns(row["id"], json.dumps(turns))
                        console.print("[dim]Skipped (edits saved).[/dim]")
                    else:
                        console.print("[dim]Skipped.[/dim]")
                    break
                elif choice in ("q", "quit"):
                    if edited:
                        store.update_scrubbed_turns(row["id"], json.dumps(turns))
                    console.print(
                        f"\n[bold]Session: {approved} approved, "
                        f"{rejected} rejected.[/bold]"
                    )
                    return
                else:
                    console.print("[dim]Enter a, r, e, s, or q.[/dim]")

        console.print(
            f"\n[bold]Review complete: {approved} approved, {rejected} rejected.[/bold]"
        )

        # Next-step hint
        stats = store.stats()
        approved_count = stats["approved"]
        if approved_count:
            console.print(
                f"\n[dim]Next: [bold]common-parlance upload[/bold] "
                f"— {approved_count} conversation(s) ready to upload[/dim]"
            )
