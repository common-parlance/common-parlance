"""`startup` command — enable or disable auto-start on login."""

from __future__ import annotations

import argparse


def run(args: argparse.Namespace) -> None:
    from rich.console import Console

    from common_parlance.autostart import (
        install_autostart,
        is_autostart_installed,
        uninstall_autostart,
    )

    console = Console()

    if args.enable:
        try:
            plat, instructions = install_autostart()
            console.print(f"[green]Auto-start enabled ({plat})[/green]")
            console.print(f"[dim]{instructions}[/dim]")
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
    elif args.disable:
        try:
            plat, was_installed = uninstall_autostart()
            if was_installed:
                console.print(f"[yellow]Auto-start disabled ({plat})[/yellow]")
            else:
                console.print("[dim]Auto-start was not enabled.[/dim]")
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
    else:
        if is_autostart_installed():
            console.print("[green]Auto-start: enabled[/green]")
            console.print("[dim]Disable with: common-parlance startup --disable[/dim]")
        else:
            console.print("[dim]Auto-start: not enabled[/dim]")
            console.print("[dim]Enable with: common-parlance startup --enable[/dim]")
