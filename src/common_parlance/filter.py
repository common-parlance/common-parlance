"""Content filtering for harmful/illegal content.

Layered approach:
1. Keyword blocklist (current) — catches obvious harmful patterns, zero dependencies
2. ML classifier (future) — optional, for auto-approve users who want stricter filtering

Legal context: CSAM is the only category with criminal liability (no Section 230
protection). Other categories (hate speech, violence) are reputational/platform-policy
risk. WildChat precedent: automated filter + responsive remediation was accepted by
HuggingFace as sufficient.

Blocklist format: plain text, one regex per line, stored in blocklists/ directory.
Category is derived from filename (e.g. csam_indicator.txt -> "csam_indicator").
Lines starting with # are comments. Blank lines are ignored.
"""

import logging
import re
from pathlib import Path
from typing import Protocol

from common_parlance.scrub import normalize_text

logger = logging.getLogger(__name__)

BLOCKLISTS_DIR = Path(__file__).parent / "blocklists"

# Leetspeak substitution table for normalizing evasion attempts.
# Maps common character substitutions back to their ASCII letter equivalents.
_LEET_MAP = str.maketrans(
    {
        "@": "a",
        "4": "a",
        "8": "b",
        "(": "c",
        "3": "e",
        "1": "i",
        "!": "i",
        "|": "l",
        "0": "o",
        "$": "s",
        "5": "s",
        "7": "t",
        "+": "t",
    }
)


_SEPARATOR_RE = re.compile(r"(?<=[a-zA-Z0-9@$!|+])[*._\-]{1,3}(?=[a-zA-Z0-9@$!|+])")


def _normalize_leet(text: str) -> str:
    """Normalize leetspeak substitutions to detect evasion attempts.

    Also collapses separator characters (*._-) between letters,
    so "f*u*c*k" or "f.u.c.k" become "fuck" after translation.
    """
    collapsed = _SEPARATOR_RE.sub("", text)
    return collapsed.translate(_LEET_MAP)


class ContentFilter(Protocol):
    """Interface for content filtering implementations."""

    def check(self, text: str) -> str | None:
        """Check text for harmful content.

        Returns None if content is safe, or a reason string if blocked.
        """
        ...


def _load_blocklist_file(path: Path) -> list[re.Pattern]:
    """Load regex patterns from a blocklist text file."""
    patterns = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(re.compile(line, re.IGNORECASE))
    return patterns


class KeywordContentFilter:
    """Blocklist-based content filter for obviously harmful patterns.

    Loads regex patterns from .txt files in the blocklists/ directory.
    Each file represents a category (filename without extension).
    """

    def __init__(self, blocklists_dir: Path = BLOCKLISTS_DIR) -> None:
        self._patterns: list[tuple[re.Pattern, str]] = []

        for path in sorted(blocklists_dir.glob("*.txt")):
            category = path.stem
            patterns = _load_blocklist_file(path)
            self._patterns.extend((p, category) for p in patterns)

        logger.info(
            "Keyword content filter initialized (%d patterns from %s)",
            len(self._patterns),
            blocklists_dir,
        )

    def check(self, text: str) -> str | None:
        """Check text against keyword blocklist.

        Checks both the original text and a leetspeak-normalized version
        to catch evasion attempts like "ch1ld p0rn" or "f*ck".
        Returns None if safe, or the matched category if blocked.
        """
        # Unicode normalization first (defeats homoglyph/zero-width evasion)
        text = normalize_text(text)
        normalized = _normalize_leet(text)
        for pattern, category in self._patterns:
            if pattern.search(text) or pattern.search(normalized):
                logger.warning("Content blocked: matched %s pattern", category)
                return category
        return None


def create_content_filter() -> ContentFilter:
    """Create the content filter."""
    return KeywordContentFilter()
