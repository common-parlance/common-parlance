"""`register` command — anonymous API key registration via device auth flow."""

from __future__ import annotations

import argparse
import contextlib
import logging
import time
import webbrowser


def run(args: argparse.Namespace) -> None:
    import httpx
    from rich.console import Console

    # Suppress httpx request logging during polling
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from common_parlance.config import load_config, save_config

    console = Console()
    config = load_config()

    # Check if already registered
    if config.get("api_key"):
        console.print("[yellow]Already registered.[/yellow]")
        console.print(
            "[dim]Your key is configured. To re-register, "
            'first clear it with: common-parlance config api_key ""[/dim]'
        )
        return

    proxy_url = config["proxy_url"]

    # Step 1: Initiate device auth flow
    console.print("[bold]Registering for an anonymous API key...[/bold]")
    try:
        resp = httpx.post(f"{proxy_url.rstrip('/')}/register/init", timeout=15.0)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        error = "Registration service unavailable."
        with contextlib.suppress(Exception):
            error = exc.response.json().get("error", error)
        console.print(f"[red]{error}[/red]")
        return
    except (httpx.ConnectError, httpx.TimeoutException):
        console.print("[red]Could not reach registration service.[/red]")
        return

    try:
        data = resp.json()
        device_code = data["device_code"]
        user_code = data["user_code"]
        verification_url = data["verification_url"]
    except (KeyError, ValueError) as exc:
        console.print(
            f"[red]Unexpected response from registration service: {exc}[/red]"
        )
        return
    poll_interval = data.get("poll_interval", 5)
    expires_in = data.get("expires_in", 600)

    # Step 2: Show code and open browser
    console.print()
    console.print(f"  Your code:  [bold cyan]{user_code}[/bold cyan]")
    console.print(f"  Open:       [link={verification_url}]{verification_url}[/link]")
    console.print()
    console.print(
        "[dim]Enter the code in your browser and complete verification.[/dim]"
    )
    console.print("[dim]Waiting...[/dim]")

    # Try to open browser automatically
    with contextlib.suppress(Exception):
        webbrowser.open(verification_url)

    # Step 3: Poll for completion
    poll_url = f"{proxy_url.rstrip('/')}/register/poll/{device_code}"
    deadline = time.monotonic() + expires_in

    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        try:
            poll_resp = httpx.get(poll_url, timeout=10.0)
            if poll_resp.status_code == 404:
                console.print("[red]Registration expired. Please try again.[/red]")
                return
            poll_data = poll_resp.json()
        except (httpx.ConnectError, httpx.TimeoutException, ValueError):
            continue  # Retry on transient/parse errors

        if poll_data.get("status") == "complete":
            api_key = poll_data["api_key"]
            config["api_key"] = api_key
            save_config(config)
            console.print()
            console.print("[green]Registration complete![/green]")
            console.print(
                "[dim]Your anonymous API key has been saved. "
                "Uploads will start automatically.[/dim]"
            )
            console.print(
                "\n[dim]Next: [bold]common-parlance consent --grant[/bold] "
                "to opt in to data contribution[/dim]"
            )
            return

    console.print("[red]Registration timed out. Please try again.[/red]")
