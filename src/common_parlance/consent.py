"""First-run consent flow.

Opt-in consent for conversation logging and contribution.
Follows the Angular CLI pattern: prompt on first run, save decision to
config, never ask again. Skip in non-TTY environments.

The raw conversations contain personal data until PII scrubbing, so we
need consent for the local processing step. Scrubbing reduces
re-identification risk but does not make the data anonymous — we say so
plainly in the consent text rather than over-claiming.
"""

import logging
import sys
from datetime import UTC, datetime

from common_parlance.config import load_config, save_config

logger = logging.getLogger(__name__)

CONSENT_TEXT = """\
[bold]Common Parlance — Data Contribution Consent[/bold]

Common Parlance collects AI conversations you choose to share and
contributes them to the Common Parlance dataset on HuggingFace,
published under the [link=https://opendatacommons.org/licenses/by/1.0/]\
ODC-BY 1.0[/link] license (Open Data Commons Attribution).

[bold]What is collected:[/bold]
  • Your conversation turns (human and assistant messages)

[bold]What is NOT collected:[/bold]
  • Model names, system prompts, or engine metadata
  • IP addresses, device info, or any client metadata
  • Names, emails, or other personal information (removed before publishing — see below)

[bold]How it works:[/bold]
  1. Import existing conversations or capture new ones via the proxy
  2. Structured PII (emails, phones, API keys) is scrubbed locally before upload
  3. You review and approve/reject conversations before they're shared
  4. On upload, the text is sent to our scrubbing service over HTTPS, which
     removes names and places server-side before publishing — no user ID or
     device metadata is attached

[bold]Important — please read:[/bold]
  • PII scrubbing is best-effort and tuned for English. Detection is strongest
    for structured data (emails, phones, keys) and English names/places;
    other-language or unusual PII can slip through. Your review is the real
    safeguard — only share conversations you're comfortable releasing publicly.
  • Scrubbing reduces re-identification risk but does not make data anonymous:
    writing style and rare details can still identify you. Treat contributions
    as public data you have chosen to release.
  • For 90 days after upload, your contributions can be removed on request.
    After that the tracking expires and they can no longer be traced to you
    or selectively removed.
  • Stop contributing at any time: [dim]common-parlance consent --revoke[/dim]
    (revoking stops future collection immediately)

[dim]Full privacy policy: https://github.com/common-parlance/common-parlance/blob/main/PRIVACY.md[/dim]
[dim]Full terms: https://github.com/common-parlance/common-parlance/blob/main/TERMS.md[/dim]
"""


def has_consent() -> bool:
    """Check if the user has given consent."""
    config = load_config()
    return config.get("consent", False) is True


def check_consent_interactive() -> bool:
    """Check consent, prompting interactively if needed.

    Returns True if consent is granted, False otherwise.
    Skips the prompt in non-TTY environments (CI, pipes, etc.).
    """
    config = load_config()

    # Already decided
    if "consent" in config:
        return config["consent"] is True

    # Non-interactive environment — don't prompt, don't collect
    if not sys.stdin.isatty():
        logger.info("Non-interactive environment detected, skipping consent prompt")
        return False

    # First run — show consent prompt
    from rich.console import Console
    from rich.prompt import Prompt

    console = Console()
    console.print()
    console.print(CONSENT_TEXT)
    console.print()

    answer = Prompt.ask(
        "[bold]Do you agree to contribute conversations?[/bold] [dim]\\[y/N][/dim]",
        default="n",
    )
    agreed = answer.strip().lower() in ("y", "yes")

    config["consent"] = agreed
    if agreed:
        config["consent_timestamp"] = datetime.now(UTC).isoformat()
    save_config(config)

    if agreed:
        console.print("[green]Consent granted. Thank you for contributing![/green]")
        console.print(
            "[dim]You can revoke anytime: common-parlance consent --revoke[/dim]"
        )
    else:
        console.print(
            "[dim]No problem. The proxy will work normally without logging.[/dim]"
        )
        console.print(
            "[dim]You can change your mind later: common-parlance consent --grant[/dim]"
        )

    console.print()
    return agreed


def grant_consent() -> None:
    """Programmatically grant consent."""
    config = load_config()
    config["consent"] = True
    config["consent_timestamp"] = datetime.now(UTC).isoformat()
    save_config(config)
    logger.info("Consent granted")


def revoke_consent() -> None:
    """Programmatically revoke consent."""
    config = load_config()
    config["consent"] = False
    config.pop("consent_timestamp", None)
    save_config(config)
    logger.info("Consent revoked — future conversations will not be logged or uploaded")
