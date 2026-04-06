"""`config` command — view and set configuration values."""

from __future__ import annotations

import argparse


def run(args: argparse.Namespace) -> None:
    from rich.console import Console
    from rich.table import Table

    from common_parlance.config import DEFAULT_CONFIG, load_config, save_config

    console = Console()
    config = load_config()

    if args.key is None:
        # Show all config
        table = Table(title="Common Parlance Config")
        table.add_column("Key", style="bold")
        table.add_column("Value")
        table.add_column("Default", style="dim")
        for key in sorted(DEFAULT_CONFIG):
            value = config.get(key, DEFAULT_CONFIG[key])
            default = str(DEFAULT_CONFIG[key])
            # Mask sensitive values
            display = str(value)
            if key == "api_key" and value:
                display = f"{'*' * (len(display) - 4)}{display[-4:]}"
            style = "" if display == default else "green"
            table.add_row(key, display, default, style=style)
        # Show consent if set
        if "consent" in config:
            table.add_row("consent", str(config["consent"]), "(not set)")
        console.print(table)
        return

    # Normalize key: accept hyphens as underscores
    key = args.key.replace("-", "_")

    if args.value is None:
        # Show single key
        if key in config:
            console.print(f"[bold]{key}[/bold] = {config[key]}")
        elif key in DEFAULT_CONFIG:
            console.print(
                f"[bold]{key}[/bold] = {DEFAULT_CONFIG[key]} [dim](default)[/dim]"
            )
        else:
            console.print(f"[red]Unknown key: {key}[/red]")
            console.print(f"[dim]Valid keys: {', '.join(sorted(DEFAULT_CONFIG))}[/dim]")
        return

    # Set key=value
    if key not in DEFAULT_CONFIG and key != "consent":
        console.print(f"[red]Unknown key: {key}[/red]")
        console.print(f"[dim]Valid keys: {', '.join(sorted(DEFAULT_CONFIG))}[/dim]")
        return

    # Parse value to match existing type
    existing = config.get(key, DEFAULT_CONFIG.get(key, ""))
    if isinstance(existing, bool):
        config[key] = args.value.lower() in ("true", "1", "yes")
    elif isinstance(existing, int):
        try:
            config[key] = int(args.value)
        except ValueError:
            console.print(f"[red]Invalid integer: {args.value}[/red]")
            return
    else:
        config[key] = args.value

    save_config(config)
    console.print(f"[green]Set {key} = {config[key]}[/green]")
