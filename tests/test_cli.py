"""Tests for CLI argument parsing and helper functions."""


def _parse_args(argv):
    """Run the CLI parser on argv, capturing SystemExit for --help/--version."""
    import argparse

    # Import the parser setup from main() by capturing it
    from common_parlance import __version__

    parser = argparse.ArgumentParser(prog="common-parlance")
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")

    # Minimal subcommands for testing parse behavior
    subparsers.add_parser("proxy")
    process_p = subparsers.add_parser("process")
    process_p.add_argument("-d", "--database", default=None)
    process_p.add_argument("--no-presidio", action="store_true")
    process_p.add_argument("-n", "--limit", type=int, default=100)
    process_p.add_argument("--auto-approve", action="store_true")

    review_p = subparsers.add_parser("review")
    review_p.add_argument("-d", "--database", default=None)
    review_p.add_argument("--approve-all", action="store_true")
    review_p.add_argument("--reset", action="store_true")

    import_p = subparsers.add_parser("import")
    import_p.add_argument("path", type=str)
    import_p.add_argument("--dry-run", action="store_true")
    import_p.add_argument("--watch", type=int, default=None)
    import_p.add_argument("--daemon", action="store_true")

    return parser.parse_args(argv)


# --- Argument parsing ---


def test_parse_process_defaults():
    args = _parse_args(["process"])
    assert args.command == "process"
    assert args.database is None
    assert args.no_presidio is False
    assert args.limit == 100
    assert args.auto_approve is False


def test_parse_process_with_flags():
    args = _parse_args(["process", "--no-presidio", "-n", "50", "--auto-approve"])
    assert args.no_presidio is True
    assert args.limit == 50
    assert args.auto_approve is True


def test_parse_review_defaults():
    args = _parse_args(["review"])
    assert args.command == "review"
    assert args.approve_all is False
    assert args.reset is False


def test_parse_review_reset():
    args = _parse_args(["review", "--reset"])
    assert args.reset is True


def test_parse_import_basic():
    args = _parse_args(["import", "/tmp/export.zip"])
    assert args.command == "import"
    assert args.path == "/tmp/export.zip"
    assert args.dry_run is False
    assert args.watch is None
    assert args.daemon is False


def test_parse_import_watch():
    args = _parse_args(["import", "/tmp/exports/", "--watch", "60", "--daemon"])
    assert args.watch == 60
    assert args.daemon is True


def test_no_command_returns_none():
    args = _parse_args([])
    assert args.command is None


# --- _edit_turns helper ---


def test_edit_turns_replaces_text():
    """Test that _edit_turns replaces text with [REDACTED]."""
    from unittest.mock import MagicMock

    from common_parlance.cli import _edit_turns

    console = MagicMock()
    turns = [
        {"role": "user", "content": "My name is Alice Smith"},
        {"role": "assistant", "content": "Hello Alice Smith!"},
    ]

    # console.input is called twice: turn number, then text to redact
    console.input = MagicMock(side_effect=["1", "Alice Smith"])

    result = _edit_turns(turns, console)
    assert result is not None
    assert "[REDACTED]" in result[0]["content"]
    assert "Alice Smith" not in result[0]["content"]
    # Original turns should be unmodified (deep copy)
    assert "Alice Smith" in turns[0]["content"]


def test_edit_turns_cancel_on_empty():
    """Test that empty turn selection cancels the edit."""
    from unittest.mock import MagicMock

    from common_parlance.cli import _edit_turns

    console = MagicMock()
    turns = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
    ]

    # Empty input for turn selection → cancel
    console.input = MagicMock(return_value="")

    result = _edit_turns(turns, console)
    assert result is None


def test_edit_turns_reject_placeholder_removal():
    """Test that trying to redact an existing placeholder is rejected."""
    from unittest.mock import MagicMock

    from common_parlance.cli import _edit_turns

    console = MagicMock()
    turns = [
        {"role": "user", "content": "Contact [EMAIL] please"},
        {"role": "assistant", "content": "OK"},
    ]

    # Select turn 1, try to redact "[EMAIL]" (should be rejected)
    console.input = MagicMock(side_effect=["1", "[EMAIL]"])

    result = _edit_turns(turns, console)
    # Should return None (placeholder removal rejected)
    assert result is None
