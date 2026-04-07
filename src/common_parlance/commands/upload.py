"""`upload` command — send approved conversations to the dataset."""

from __future__ import annotations

import argparse
from pathlib import Path

from ._shared import resolve_db


def _preflight_checks(console, config, proxy_url: str, api_key: str) -> bool:
    """Verify consent, API key, and NER service before uploading.

    Returns True if all checks pass, False otherwise.
    """
    import httpx

    from common_parlance.consent import has_consent

    # 1. Consent
    if not has_consent():
        console.print(
            "[red]Consent not granted.[/red]\n"
            "[dim]Grant consent with: common-parlance consent --grant[/dim]"
        )
        return False

    # 2. API key
    if not api_key:
        console.print(
            "[red]No API key configured.[/red]\n"
            "[dim]Register with: common-parlance register[/dim]"
        )
        return False

    # 3. Worker health
    try:
        resp = httpx.get(f"{proxy_url.rstrip('/')}/health", timeout=10.0)
        if resp.status_code != 200:
            console.print(
                "[red]Upload service is unreachable.[/red]\n"
                f"[dim]{proxy_url}/health returned HTTP {resp.status_code}[/dim]"
            )
            return False
    except (httpx.ConnectError, httpx.TimeoutException):
        console.print(
            "[red]Upload service is unreachable.[/red]\n"
            f"[dim]Could not connect to {proxy_url}[/dim]"
        )
        return False

    # 4. NER service health — wake it up and wait for it to be ready
    ner_url = config.get("ner_health_url", "https://common-parlance-ner-service.hf.space")
    console.print("[dim]Checking NER service...[/dim]", end="")
    ner_ready = False
    for attempt in range(6):  # up to ~60s for cold start
        try:
            resp = httpx.get(f"{ner_url.rstrip('/')}/health", timeout=15.0)
            if resp.status_code == 200:
                ner_ready = True
                break
        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        if attempt < 5:
            console.print("[dim].[/dim]", end="")
            import time

            time.sleep(10)

    if not ner_ready:
        console.print()
        console.print(
            "\n[red]NER service is not responding.[/red]\n"
            "[dim]The NER service scrubs names and locations from your data.\n"
            "It may be waking up from sleep (HuggingFace free tier).\n"
            "Please wait a minute and try again.[/dim]"
        )
        return False

    console.print(" [green]ready[/green]")
    return True


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

    if not _preflight_checks(console, config, proxy_url, api_key):
        return

    with ConversationStore(db_path) as store:
        from rich.status import Status

        with Status(
            "Uploading conversations (NER scrubbing may take a moment)...",
            console=console,
        ) as status:
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
