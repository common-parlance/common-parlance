"""Shared helpers used by multiple CLI command implementations."""

from __future__ import annotations

import argparse


def resolve_db(args: argparse.Namespace) -> str:
    """Resolve database path: CLI flag > config > default."""
    from common_parlance.config import DEFAULT_CONFIG, load_config

    # argparse default is None (sentinel), so we know if user passed -d
    if getattr(args, "database", None) is not None:
        return args.database
    config = load_config()
    return config.get("database", DEFAULT_CONFIG["database"])
