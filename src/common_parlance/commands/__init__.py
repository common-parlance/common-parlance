"""CLI command implementations.

Each module exposes a ``run(args)`` function that executes one subcommand.
The ``REGISTRY`` maps subcommand names (as registered with argparse) to
their ``run`` functions.
"""

from __future__ import annotations

from collections.abc import Callable

from . import (
    audit_cmd,
    config_cmd,
    consent_cmd,
    dead_letters,
    import_cmd,
    process,
    proxy,
    register,
    review,
    startup,
    status,
    upload,
)

REGISTRY: dict[str, Callable] = {
    "proxy": proxy.run,
    "process": process.run,
    "review": review.run,
    "config": config_cmd.run,
    "upload": upload.run,
    "register": register.run,
    "consent": consent_cmd.run,
    "startup": startup.run,
    "dead-letters": dead_letters.run,
    "import": import_cmd.run,
    "audit": audit_cmd.run,
    "status": status.run,
}

# Re-exports for backwards-compatible test imports
_edit_turns = review._edit_turns
_print_audit = audit_cmd._print_audit

__all__ = ["REGISTRY", "_edit_turns", "_print_audit"]
