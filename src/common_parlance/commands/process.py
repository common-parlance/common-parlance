"""`process` command — scrub PII and run a leakage audit."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ._shared import resolve_db


def run(args: argparse.Namespace) -> None:
    from rich.console import Console

    from common_parlance.config import load_config, save_config
    from common_parlance.db import ConversationStore
    from common_parlance.filter import CompositeContentFilter, create_content_filter
    from common_parlance.process import process_batch
    from common_parlance.scrub import create_scrubber

    console = Console()
    config = load_config()
    db_path = resolve_db(args)

    # use_presidio: CLI flag overrides config
    use_presidio = not args.no_presidio and config.get("use_presidio", True)

    scrubber = create_scrubber(use_presidio=use_presidio)

    if not scrubber.has_ner and use_presidio:
        # First time: ask if they want to install NER
        if not config.get("ner_prompted"):
            console.print(
                "\n[yellow]Local NER (name/address detection)"
                " is not installed.[/yellow]\n"
                "[dim]Server-side NER adds another pass before"
                " publishing, but no automated pass catches every"
                " name —\nreview each conversation before approving."
                " Installing local NER adds an extra scrubbing"
                " layer on your machine.[/dim]\n"
            )

            if sys.stdin.isatty():
                from rich.prompt import Prompt

                answer = Prompt.ask(
                    "[bold]Would you like to install local NER?"
                    " (~380MB)[/bold] [dim]\\[y/N][/dim]",
                    default="n",
                )
                install = answer.strip().lower() in ("y", "yes")
                config["ner_prompted"] = True
                if install:
                    config["use_presidio"] = True
                    save_config(config)
                    console.print(
                        "\n[bold]Run these commands, then re-run process:[/bold]\n"
                        "  uv pip install presidio-analyzer presidio-anonymizer spacy\n"
                        "  python -m spacy download en_core_web_lg\n"
                    )
                    return
                else:
                    config["use_presidio"] = False
                    save_config(config)
                    console.print(
                        "[dim]Continuing without local NER."
                        " Server-side NER will handle"
                        " names.\nChange later with:"
                        " common-parlance config"
                        " use_presidio true[/dim]\n"
                    )
            else:
                config["ner_prompted"] = True
                save_config(config)
        else:
            # Already prompted, just show a brief note
            console.print(
                "[dim]Running without local NER (regex-only). "
                "Change with: common-parlance config use_presidio true[/dim]"
            )

    if not Path(db_path).exists():
        console.print(
            "[dim]No database yet. Import or capture some conversations first.[/dim]"
        )
        return

    with ConversationStore(db_path) as store:
        # ML content filter (Detoxify) layers on top of the keyword blocklist
        # when the [ml] extra is installed; on by default, opt out with
        # `common-parlance config use_ml_filter false`.
        use_ml = config.get("use_ml_filter", True)
        content_filter = create_content_filter(use_ml=use_ml)
        if isinstance(content_filter, CompositeContentFilter):
            console.print("[dim]ML content filter active (Detoxify).[/dim]")

        console.print(f"[bold]Processing up to {args.limit} exchanges...[/bold]")
        count = process_batch(
            store, scrubber, limit=args.limit, content_filter=content_filter
        )
        console.print(f"[green]Processed {count} exchanges.[/green]")

        # Quick leakage scan on newly staged conversations
        if count > 0:
            from common_parlance.audit import audit_conversations

            from .audit_cmd import _print_audit

            pending = store.conn.execute(
                "SELECT id, scrubbed_turns FROM staged "
                "WHERE approved = 0 AND uploaded = 0 ORDER BY created_at"
            ).fetchall()
            if pending:
                console.print()
                result = audit_conversations(pending)
                _print_audit(result, console, brief=True)

        # Warn about non-English conversations: both the local and server-side
        # NER passes are English-only, so PII detection is weaker for other
        # languages. Surface this before review/auto-approve so the user knows
        # to look harder at (or skip) those conversations. "unknown" is left out
        # because it is ambiguous (short/code-heavy text), not a positive signal
        # of non-English content.
        lang_counts = store.pending_language_counts()
        non_english = {
            lang: n for lang, n in lang_counts.items() if lang not in ("en", "unknown")
        }
        if non_english:
            total = sum(non_english.values())
            summary = ", ".join(
                f"{lang} ({n})"
                for lang, n in sorted(non_english.items(), key=lambda kv: -kv[1])
            )
            console.print(
                f"\n[bold yellow]⚠ {total} pending conversation(s) detected as "
                f"non-English ({summary}).[/bold yellow]"
            )
            console.print(
                "[yellow]  PII scrubbing (regex + NER) is tuned for English; "
                "names, places, and other PII in other languages are more likely "
                "to survive. Review these especially carefully before "
                "approving.[/yellow]"
            )

        # Check persistent config if CLI flag not set
        auto_approve = args.auto_approve or config.get("auto_approve", False)

        if auto_approve:
            pending = store.get_pending_review()
            if pending:
                store.approve_batch([row["id"] for row in pending])
                # Loud, not reassuring: auto-approve skips the human review that
                # is the last line of defense when the automated filters miss
                # something. Make the bypass and the shifted responsibility
                # explicit every run, rather than a green "all good" message.
                console.print(
                    f"[bold yellow]⚠ Auto-approved {len(pending)} "
                    f"conversation(s) WITHOUT human review.[/bold yellow]"
                )
                console.print(
                    "[yellow]  The content/PII filters catch most issues but "
                    "miss some; with review skipped, anything they miss will be "
                    "uploaded. You are responsible for what you publish.\n"
                    "  Disable with: [bold]common-parlance config auto_approve "
                    "false[/bold][/yellow]"
                )

        # Next-step hint
        stats = store.stats()
        pending_count = stats["pending_review"]
        approved_count = stats["approved"]
        if pending_count:
            console.print(
                f"\n[dim]Next: [bold]common-parlance review[/bold] "
                f"— {pending_count} conversation(s) pending review[/dim]"
            )
        elif approved_count:
            console.print(
                f"\n[dim]Next: [bold]common-parlance upload[/bold] "
                f"— {approved_count} conversation(s) ready to upload[/dim]"
            )
