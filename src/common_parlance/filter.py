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
import unicodedata
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


# Collapse runs of single characters separated by 1-3 spaces/punctuation
# ("c h i l d p o r n" -> "childporn"). Only *runs of single chars* are
# collapsed: whole-word spacing ("child porn") is already caught by the \s* in
# the patterns and must NOT be touched here, because blanket whitespace removal
# would fuse normal prose ("child sex education" -> "childsexeducation") and
# mass-false-block legitimate text. The boundary lookarounds force every char in
# the run to be single; the bounded {1,3}/{2,} quantifiers keep it linear (no
# catastrophic backtracking — same ReDoS guard as the other gate patterns).
# Mirrors content-filter.js collapseSpaced() byte-for-byte.
_SPACED_RUN_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9](?:[\s*._\-]{1,3}[A-Za-z0-9]){2,}(?![A-Za-z0-9])"
)
_SPACED_SEP_RE = re.compile(r"[\s*._\-]+")


def _collapse_spaced(text: str) -> str:
    """Collapse single-character-spacing evasion ("c h i l d" -> "child")."""
    return _SPACED_RUN_RE.sub(lambda m: _SPACED_SEP_RE.sub("", m.group(0)), text)


def _strip_combining(text: str) -> str:
    """Drop Unicode combining marks (Zalgo / stacked-diacritic evasion).

    NFKC (in normalize_text) does NOT remove combining marks, so
    "çḥíl̀d" survives the rest of the pipeline. Decompose
    (NFD) and drop every mark (category M*) so the base letters re-form the word
    for matching. Match-key only — the stored text is untouched. Mirrors
    content-filter.js stripMarks() (\\p{M}).
    """
    return "".join(
        c
        for c in unicodedata.normalize("NFD", text)
        if not unicodedata.category(c).startswith("M")
    )


# Cross-script homoglyphs → ASCII (curated Cyrillic + Greek skeleton). NFKC does
# NOT fold these, so the blocklist would be bypassed by a single lookalike
# (Cyrillic о in "bоmb"). Applied ONLY in the content-filter check() — NOT in the
# shared normalize_text, where homoglyph-folding is a deliberate PII wontfix.
_CONFUSABLES = str.maketrans(
    {
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "у": "y",
        "х": "x",
        "к": "k",
        "м": "m",
        "т": "t",
        "н": "h",
        "в": "b",
        "і": "i",
        "ј": "j",
        "ѕ": "s",
        "ԁ": "d",
        "А": "A",
        "Е": "E",
        "О": "O",
        "Р": "P",
        "С": "C",
        "У": "Y",
        "Х": "X",
        "К": "K",
        "М": "M",
        "Т": "T",
        "Н": "H",
        "В": "B",
        "І": "I",
        "ο": "o",
        "α": "a",
        "ε": "e",
        "ι": "i",
        "ν": "v",
        "ρ": "p",
        "τ": "t",
        "κ": "k",
        "χ": "x",
        "υ": "u",
        "Ο": "O",
        "Α": "A",
        "Ε": "E",
        "Ι": "I",
        "Ν": "N",
        "Ρ": "P",
        "Τ": "T",
        "Κ": "K",
        "Χ": "X",
        "Β": "B",
        "Ζ": "Z",
        "Η": "H",
        "Μ": "M",
    }
)


def _fold_confusables(text: str) -> str:
    """Map common cross-script homoglyphs to their ASCII lookalikes."""
    return text.translate(_CONFUSABLES)


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
        # Normalize, strip combining marks, fold cross-script homoglyphs,
        # collapse single-char spacing, then leet — match each form.
        text = normalize_text(text)
        folded = _fold_confusables(_strip_combining(text))
        normalized = _normalize_leet(_collapse_spaced(folded))
        for pattern, category in self._patterns:
            if (
                pattern.search(text)
                or pattern.search(folded)
                or pattern.search(normalized)
            ):
                logger.warning("Content blocked: matched %s pattern", category)
                return category
        return None


class CompositeContentFilter:
    """Run several content filters in order, stopping at the first block.

    Cheaper, deterministic filters (the keyword blocklist) come first so the
    expensive ML pass only runs on content that already cleared them.
    """

    def __init__(self, filters: list[ContentFilter]) -> None:
        self._filters = filters

    def check(self, text: str) -> str | None:
        for content_filter in self._filters:
            reason = content_filter.check(text)
            if reason is not None:
                return reason
        return None


def create_content_filter(use_ml: bool = True) -> ContentFilter:
    """Create the content filter.

    The keyword blocklist is always present. When ``use_ml`` is true and the
    optional ``detoxify`` dependency (the ``[ml]`` extra) is installed, an ML
    toxicity filter is layered on top to catch contextual toxicity that keyword
    matching misses (e.g. spaced-out or paraphrased terms). If detoxify is not
    installed, the keyword filter is used alone — graceful degradation, no error.
    """
    keyword = KeywordContentFilter()
    if not use_ml:
        return keyword

    # Local import so the keyword-only path never touches the ML module.
    from common_parlance.filter_ml import create_ml_content_filter

    ml = create_ml_content_filter()
    if ml is None:
        return keyword
    return CompositeContentFilter([keyword, ml])
