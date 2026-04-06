"""First-run consent flow.

Opt-in consent for conversation logging and contribution.
Follows the Angular CLI pattern: prompt on first run, save decision to
config, never ask again. Skip in non-TTY environments.

The raw conversations contain personal data until PII scrubbing, so we
need consent for the local processing step. Once scrubbed and uploaded,
data is anonymous — but we're transparent about that.
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
  • Names, emails, or other personal information (scrubbed before upload)

[bold]How it works:[/bold]
  1. Import existing conversations or capture new ones via the proxy
  2. PII (names, emails, phones, etc.) is scrubbed locally before upload
  3. You can review and approve/reject conversations before they're shared
  4. Approved conversations are uploaded anonymously — no user ID attached

[bold]Important:[/bold]
  • For 90 days after upload, your contributions can be removed on request
  • After 90 days, the tracking expires and contributions become permanently anonymous
  • You can stop contributing at any time: [dim]common-parlance consent --revoke[/dim]
  • Revoking consent stops future collection immediately

[dim]Full privacy policy: https://github.com/common-parlance/common-parlance/blob/main/PRIVACY.md[/dim]
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
