"""CLI entry point for Common Parlance.

Argparse definitions live here; each subcommand's implementation lives in
``common_parlance.commands.<name>`` and exposes a ``run(args)`` function.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys

# Ensure system pager preserves Rich ANSI colors and behaves well
# -R: pass ANSI codes through, -F: quit if fits one screen, -X: no clear on exit
os.environ.setdefault("LESS", "-RFX")

# Re-exports for backwards-compatible imports (tests/users may import these)
from common_parlance.commands import _edit_turns, _print_audit  # noqa: F401


def main() -> None:
    from common_parlance import __version__
    from common_parlance.commands import REGISTRY

    parser = argparse.ArgumentParser(
        prog="common-parlance",
        description="Privacy-preserving proxy for open AI conversation commons",
        epilog=(
            "Quickstart: common-parlance consent --grant && "
            "common-parlance proxy\n"
            "Workflow:   proxy -> process -> review -> upload"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command")

    # --- proxy command ---
    proxy_parser = subparsers.add_parser(
        "proxy", help="Start the transparent proxy (intercepts AI conversations)"
    )
    proxy_parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=None,
        help="Port to listen on (default: from config, 11435)",
    )
    proxy_parser.add_argument(
        "-u",
        "--upstream",
        default=None,
        help="Upstream model endpoint (default: from config)",
    )
    proxy_parser.add_argument(
        "-d", "--database", default=None, help="SQLite database path"
    )
    proxy_parser.add_argument(
        "--no-log", action="store_true", help="Disable conversation logging"
    )

    # --- process command ---
    process_parser = subparsers.add_parser(
        "process", help="Scrub PII from logged conversations"
    )
    process_parser.add_argument(
        "-d", "--database", default=None, help="SQLite database path"
    )
    process_parser.add_argument(
        "--no-presidio",
        action="store_true",
        help="Use regex-only scrubber (server-side NER handles names)",
    )
    process_parser.add_argument(
        "-n", "--limit", type=int, default=100, help="Max exchanges to process"
    )
    process_parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Automatically approve after scrubbing (skip review step)",
    )

    # --- review command ---
    review_parser = subparsers.add_parser(
        "review", help="Review and approve/reject staged conversations"
    )
    review_parser.add_argument(
        "-d", "--database", default=None, help="SQLite database path"
    )
    review_parser.add_argument(
        "--approve-all",
        action="store_true",
        help="Approve all pending conversations without review",
    )
    review_parser.add_argument(
        "--reset",
        action="store_true",
        help="Send approved conversations back to review (un-approve)",
    )

    # --- config command ---
    config_parser = subparsers.add_parser(
        "config", help="View or set persistent preferences"
    )
    config_parser.add_argument(
        "key",
        nargs="?",
        help="Config key (e.g. auto_approve, port, upstream)",
    )
    config_parser.add_argument(
        "value",
        nargs="?",
        help="Value to set (e.g. true, 11436)",
    )

    # --- upload command ---
    upload_parser = subparsers.add_parser(
        "upload", help="Upload approved conversations to the dataset"
    )
    upload_parser.add_argument(
        "-d", "--database", default=None, help="SQLite database path"
    )
    upload_parser.add_argument(
        "-n", "--limit", type=int, default=500, help="Max conversations per batch"
    )

    # --- register command ---
    subparsers.add_parser(
        "register",
        help="Register for an anonymous API key (no account needed)",
    )

    # --- consent command ---
    consent_parser = subparsers.add_parser(
        "consent", help="Manage data contribution consent"
    )
    consent_group = consent_parser.add_mutually_exclusive_group()
    consent_group.add_argument(
        "--grant", action="store_true", help="Grant consent to contribute"
    )
    consent_group.add_argument(
        "--revoke", action="store_true", help="Revoke consent (stop contributing)"
    )
    consent_parser.add_argument(
        "-d", "--database", default=None, help="SQLite database path"
    )

    # --- startup command ---
    startup_parser = subparsers.add_parser(
        "startup", help="Configure auto-start on login"
    )
    startup_group = startup_parser.add_mutually_exclusive_group()
    startup_group.add_argument(
        "--enable", action="store_true", help="Enable auto-start on login"
    )
    startup_group.add_argument(
        "--disable", action="store_true", help="Disable auto-start"
    )

    # --- dead-letters command ---
    dl_parser = subparsers.add_parser(
        "dead-letters",
        help="List or retry conversations that failed upload repeatedly",
    )
    dl_parser.add_argument(
        "-d", "--database", default=None, help="SQLite database path"
    )
    dl_parser.add_argument(
        "--retry",
        action="store_true",
        help="Reset failure counts so dead-lettered conversations retry",
    )

    # --- import command ---
    import_parser = subparsers.add_parser(
        "import",
        help="Import conversations from external file formats",
    )
    import_parser.add_argument(
        "path",
        type=str,
        help="File or directory to import conversations from",
    )
    import_parser.add_argument(
        "-d", "--database", default=None, help="SQLite database path"
    )
    import_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be imported without writing to database",
    )
    import_parser.add_argument(
        "--watch",
        type=int,
        metavar="MINUTES",
        default=None,
        help="Re-scan on interval (e.g. --watch 60 for hourly)",
    )
    import_parser.add_argument(
        "--daemon",
        action="store_true",
        help="Install --watch as a system service that survives restarts",
    )

    # --- audit command ---
    audit_parser = subparsers.add_parser(
        "audit", help="Quick PII leakage scan and over-redaction check"
    )
    audit_parser.add_argument(
        "-d", "--database", default=None, help="SQLite database path"
    )
    audit_parser.add_argument(
        "--all",
        action="store_true",
        help="Scan all staged (including already uploaded)",
    )
    audit_parser.add_argument(
        "--brief", action="store_true", help="Compact summary (used by process)"
    )

    # --- status command ---
    status_parser = subparsers.add_parser(
        "status", help="Show pipeline and system status"
    )
    status_parser.add_argument(
        "-d", "--database", default=None, help="SQLite database path"
    )

    args = parser.parse_args()

    # No command = show help
    if args.command is None:
        parser.print_help()
        sys.exit(0)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        REGISTRY[args.command](args)
    except KeyboardInterrupt:
        print()
    except sqlite3.Error as exc:
        logging.getLogger(__name__).debug("Database error", exc_info=True)
        print(f"Database error: {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        logging.getLogger(__name__).debug("OS error", exc_info=True)
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        logging.getLogger(__name__).debug("Unexpected error", exc_info=True)
        print(
            f"Unexpected error ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
