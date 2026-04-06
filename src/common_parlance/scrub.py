"""PII scrubbing pipeline.

Uses Presidio for NER-based detection (names, addresses, organizations)
and regex-based detection (emails, phones, credit cards, SSNs, IPs).
Replaces detected PII with typed placeholders: [NAME_1], [EMAIL], etc.
"""

import logging
import math
import re
import unicodedata
from collections import Counter
from typing import Protocol

logger = logging.getLogger(__name__)

# --- Unicode normalization (adversarial PII evasion defense) ---
# Homoglyphs (Cyrillic а vs Latin a), zero-width characters, and bidi
# overrides can bypass both regex and NER. NFKC normalization + control
# character stripping defeats these attacks. Must run before any pattern
# matching or NER analysis.
# Reference: "Unmasking the Reality of PII Masking Models" (arXiv:2504.12308)

# Invisible/control characters that can break tokenization without
# changing visible text. Covers zero-width spaces, joiners, bidi
# overrides, and other Unicode format characters.
_INVISIBLE_RE = re.compile(
    "["
    "\u200b"  # zero-width space
    "\u200c"  # zero-width non-joiner
    "\u200d"  # zero-width joiner
    "\u200e"  # left-to-right mark
    "\u200f"  # right-to-left mark
    "\u202a"  # left-to-right embedding
    "\u202b"  # right-to-left embedding
    "\u202c"  # pop directional formatting
    "\u202d"  # left-to-right override
    "\u202e"  # right-to-left override
    "\u2060"  # word joiner
    "\u2061"  # function application
    "\u2062"  # invisible times
    "\u2063"  # invisible separator
    "\u2064"  # invisible plus
    "\ufeff"  # byte order mark / zero-width no-break space
    "\ufff9"  # interlinear annotation anchor
    "\ufffa"  # interlinear annotation separator
    "\ufffb"  # interlinear annotation terminator
    "]+",
)


def normalize_text(text: str) -> str:
    """Normalize text to defeat adversarial PII evasion.

    1. NFKC normalization — maps homoglyphs to canonical Latin forms
       (Cyrillic а → a, fullwidth Ａ → A, mathematical 𝐀 → A)
    2. Strip invisible characters — removes zero-width spaces, joiners,
       bidi overrides, and other Unicode control characters that break
       tokenization without changing visible text.

    This MUST run before any regex matching or NER analysis.
    """
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE_RE.sub("", text)
    return text


# --- High-entropy secret detection ---

# Known API key prefixes (vendor-specific patterns)
_SECRET_PREFIX_PATTERNS = [
    re.compile(r"\b(sk-[a-zA-Z0-9]{20,})\b"),  # OpenAI
    re.compile(r"\b(sk-ant-[a-zA-Z0-9\-]{20,})\b"),  # Anthropic
    re.compile(r"\b(ghp_[a-zA-Z0-9]{36,})\b"),  # GitHub PAT
    re.compile(r"\b(gho_[a-zA-Z0-9]{36,})\b"),  # GitHub OAuth
    re.compile(r"\b(glpat-[a-zA-Z0-9\-_]{20,})\b"),  # GitLab PAT
    re.compile(r"\b(xoxb-[a-zA-Z0-9\-]{20,})\b"),  # Slack bot
    re.compile(r"\b(xoxp-[a-zA-Z0-9\-]{20,})\b"),  # Slack user
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),  # AWS access key
    re.compile(r"\b(hf_[a-zA-Z0-9]{20,})\b"),  # HuggingFace
    re.compile(r"\b(npm_[a-zA-Z0-9]{36,})\b"),  # npm
    re.compile(r"\b(pypi-[a-zA-Z0-9]{20,})\b"),  # PyPI
    re.compile(r"\b(AIza[a-zA-Z0-9\-_]{35})\b"),  # Google Cloud
    re.compile(r"\b(sk_live_[a-zA-Z0-9]{24,})\b"),  # Stripe secret
    re.compile(r"\b(pk_live_[a-zA-Z0-9]{24,})\b"),  # Stripe public
    re.compile(r"\b(rk_live_[a-zA-Z0-9]{24,})\b"),  # Stripe restricted
    re.compile(r"\b(SG\.[a-zA-Z0-9\-_]{22,})\b"),  # SendGrid
    re.compile(r"\b(dop_v1_[a-zA-Z0-9]{64})\b"),  # DigitalOcean
    re.compile(r"\b(eyJ[a-zA-Z0-9\-_]{20,}\.eyJ[a-zA-Z0-9\-_]{20,})\b"),  # JWT
]

# Multi-line secret patterns (private keys, connection strings)
_SECRET_BLOCK_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
    re.compile(r"-----BEGIN PGP PRIVATE KEY BLOCK-----"),
    re.compile(
        r"\b(?:postgres|mysql|mongodb|redis)://[^\s]+:[^\s]+@[^\s]+"
    ),  # DB connection strings
    re.compile(r"Authorization:\s*Bearer\s+[a-zA-Z0-9\-_.]+", re.IGNORECASE),
]

# File path patterns that leak usernames and directory structure
_FILE_PATH_PATTERNS = [
    re.compile(r"/Users/[a-zA-Z0-9_.-]+(?:/[^\s\"'`,;)}\]]+)?"),  # macOS
    re.compile(r"/home/[a-zA-Z0-9_.-]+(?:/[^\s\"'`,;)}\]]+)?"),  # Linux
    re.compile(
        r"C:\\Users\\[a-zA-Z0-9_.-]+(?:\\[^\s\"'`,;)}\]]+)?", re.IGNORECASE
    ),  # Windows
    re.compile(r"/root(?:/[^\s\"'`,;)}\]]+)?"),  # root home
]

# Generic high-entropy token pattern: long alphanumeric strings that
# look like secrets (mixed case + digits, 24+ chars, not common words)
_GENERIC_TOKEN_RE = re.compile(r"\b([a-zA-Z0-9_\-]{24,})\b")


def _shannon_entropy(s: str) -> float:
    """Calculate Shannon entropy of a string in bits per character."""
    if not s:
        return 0.0
    length = len(s)
    return -sum((c / length) * math.log2(c / length) for c in Counter(s).values())


def _check_entropy(match: re.Match) -> str:
    """Replace high-entropy tokens with [SECRET]."""
    token = match.group(1)
    if _shannon_entropy(token) > 4.0:
        return "[SECRET]"
    return token


def scrub_secrets(text: str) -> str:
    """Detect and replace API keys, tokens, and high-entropy secrets."""
    result = text

    # Pass 1: known vendor prefixes (high confidence)
    for pattern in _SECRET_PREFIX_PATTERNS:
        result = pattern.sub("[SECRET]", result)

    # Pass 1b: multi-line/structural secret patterns
    for pattern in _SECRET_BLOCK_PATTERNS:
        result = pattern.sub("[SECRET]", result)

    # Pass 1c: file paths that leak usernames
    for pattern in _FILE_PATH_PATTERNS:
        result = pattern.sub("[PATH]", result)

    # Pass 2: generic high-entropy tokens (conservative threshold)
    # Only flag strings with entropy > 4.0 bits/char (random alphanumeric
    # averages ~5.2; English text averages ~3.5-4.0)
    result = _GENERIC_TOKEN_RE.sub(_check_entropy, result)
    return result


# --- URL reduction ---
# Preserve domain context (useful for technical discussions) but strip
# full paths, query strings, and credentials to prevent SEO/spam abuse.
_URL_RE = re.compile(r"https?://(?:[^\s/@]+@)?([^\s/:?#]+)[^\s]*", re.IGNORECASE)
# Known scammy/spam TLDs — flag these entirely
_SPAM_TLDS = frozenset(
    {
        "xyz",
        "click",
        "top",
        "buzz",
        "gq",
        "ml",
        "cf",
        "tk",
        "ga",
        "work",
        "loan",
        "download",
        "racing",
        "win",
        "bid",
        "stream",
        "icu",
        "monster",
        "rest",
        "hair",
        "beauty",
        "sbs",
        "cfd",
    }
)


def _reduce_url(match: re.Match) -> str:
    """Replace URL with [URL:domain] or [URL:suspicious] for spam TLDs."""
    domain = match.group(1).lower()
    tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
    if tld in _SPAM_TLDS:
        return "[URL:suspicious]"
    return f"[URL:{domain}]"


def scrub_urls(text: str) -> str:
    """Reduce URLs to [URL:domain] to preserve context without abusable links."""
    return _URL_RE.sub(_reduce_url, text)


class Scrubber(Protocol):
    """Interface for PII scrubbing implementations."""

    @property
    def has_ner(self) -> bool:
        """Whether this scrubber has NER-based name detection."""
        ...

    def scrub(self, text: str) -> str:
        """Scrub PII from text, returning cleaned version with placeholders."""
        ...


# Programming terms that Presidio's spaCy NER misidentifies as entities.
# "Python" → ORGANIZATION, "Git" → PERSON, "C" → PERSON, etc.
# Presidio's allow_list excludes exact case-insensitive matches.
# Curated from false positives observed in real AI conversation data.
_PROGRAMMING_ALLOW_LIST = [
    # Languages often misidentified as PERSON or ORGANIZATION
    "Python",
    "Java",
    "Ruby",
    "Rust",
    "Swift",
    "Kotlin",
    "Scala",
    "Julia",
    "Perl",
    "Lua",
    "Dart",
    "Elixir",
    "Fortran",
    "Pascal",
    "Haskell",
    "Erlang",
    "Clojure",
    "Groovy",
    "C",
    "R",
    "Go",
    # Tools and platforms misidentified as PERSON
    "Git",
    "Docker",
    "Kubernetes",
    "Terraform",
    "Ansible",
    "Jenkins",
    "Gradle",
    "Maven",
    "Cargo",
    "Helm",
    "Vagrant",
    "Nginx",
    "Apache",
    "Redis",
    "Kafka",
    "Celery",
    "Pandas",
    "NumPy",
    "Flask",
    "Django",
    "FastAPI",
    "Rails",
    "Spring",
    "Node",
    "Deno",
    "Bun",
    # Common CS terms misidentified as PERSON/ORG
    "Boolean",
    "Lambda",
    "Mutex",
    "Regex",
    # AI/ML terms
    "Transformer",
    "BERT",
    "GPT",
    "LLM",
    "CUDA",
    "PyTorch",
    "TensorFlow",
    "Keras",
    "Llama",
    "Claude",
    "Gemini",
    # Frameworks and libraries
    "React",
    "Angular",
    "Vue",
    "Svelte",
    "jQuery",
    "Bootstrap",
    "Tailwind",
    "Express",
    "Nest",
    "Next",
    "Nuxt",
    "Remix",
    # Math/algorithm terms
    "Fibonacci",
    "Dijkstra",
    "Euler",
]

# Per-entity score thresholds. Presidio's analyze() only takes a single
# global threshold, so we post-filter results with stricter per-entity
# thresholds. Values chosen based on false positive analysis:
# - PERSON at 0.5 flags programming terms even with allow_list
# - DATE_TIME at 0.5 flags version numbers, code timestamps
_ENTITY_SCORE_THRESHOLDS = {
    "PERSON": 0.85,
    "DATE_TIME": 0.80,
    "NRP": 0.80,
    "LOCATION": 0.70,
}
_DEFAULT_SCORE_THRESHOLD = 0.5


class PresidioScrubber:
    """PII scrubber using Microsoft Presidio + spaCy NER."""

    has_ner = True

    def __init__(self, model_name: str = "en_core_web_lg"):
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        from presidio_anonymizer import AnonymizerEngine
        from presidio_anonymizer.entities import OperatorConfig

        # Silence noisy Presidio warnings (non-English recognizers, unmapped
        # spaCy entity types like CARDINAL/PRODUCT/ORDINAL)
        logging.getLogger("presidio-analyzer").setLevel(logging.ERROR)

        nlp_provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": model_name}],
            }
        )
        self._analyzer = AnalyzerEngine(
            nlp_engine=nlp_provider.create_engine(),
            supported_languages=["en"],
        )
        self._anonymizer = AnonymizerEngine()

        # Map Presidio entity types to our placeholder names.
        self._type_map = {
            "PERSON": "NAME",
            "EMAIL_ADDRESS": "EMAIL",
            "PHONE_NUMBER": "PHONE",
            "CREDIT_CARD": "CREDIT_CARD",
            "US_SSN": "SSN",
            "IP_ADDRESS": "IP",
            "LOCATION": "LOCATION",
            "URL": "URL",
            "IBAN_CODE": "IBAN",
            "NRP": "GROUP",
            "MEDICAL_LICENSE": "MEDICAL_ID",
            "US_DRIVER_LICENSE": "DRIVER_LICENSE",
            "DATE_TIME": "DATE",
        }

        self._operators = {
            entity_type: OperatorConfig("replace", {"new_value": f"[{placeholder}]"})
            for entity_type, placeholder in self._type_map.items()
        }

        # Presidio's allow_list is case-sensitive, so include both
        # original casing and lowercase to catch "Git" and "git".
        self._allow_list = list(
            {t for term in _PROGRAMMING_ALLOW_LIST for t in (term, term.lower())}
        )

        logger.info("Presidio scrubber initialized with model: %s", model_name)

    def _filter_results(self, results: list, text: str) -> list:
        """Apply per-entity score thresholds and allow list filtering."""
        filtered = []
        for result in results:
            threshold = _ENTITY_SCORE_THRESHOLDS.get(
                result.entity_type, _DEFAULT_SCORE_THRESHOLD
            )
            if result.score < threshold:
                entity_text = text[result.start : result.end]
                logger.debug(
                    "Filtered low-confidence %s: '%s' (score=%.2f, threshold=%.2f)",
                    result.entity_type,
                    entity_text,
                    result.score,
                    threshold,
                )
                continue
            filtered.append(result)
        return filtered

    def scrub(self, text: str) -> str:
        """Scrub PII from text using Presidio, plus secret detection."""
        # Unicode normalization first (defeats homoglyph/zero-width evasion)
        text = normalize_text(text)
        # URL reduction (before Presidio sees URLs as false-positive names)
        text = scrub_urls(text)
        # Secret scanning (Presidio doesn't catch API keys/tokens)
        text = scrub_secrets(text)

        results = self._analyzer.analyze(
            text=text,
            language="en",
            score_threshold=_DEFAULT_SCORE_THRESHOLD,
            allow_list=self._allow_list,
        )

        # Apply stricter per-entity thresholds
        results = self._filter_results(results, text)

        if not results:
            return text

        anonymized = self._anonymizer.anonymize(
            text=text,
            analyzer_results=results,
            operators=self._operators,
        )

        # Run regex patterns as safety net (catches what NER misses)
        result = anonymized.text
        result = _CC_RE.sub(_luhn_check, result)
        for pattern, replacement in _PII_PATTERNS:
            result = pattern.sub(replacement, result)
        return result


_CC_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")


def _luhn_check(match: re.Match) -> str:
    """Replace credit card numbers verified by Luhn checksum."""
    digits = [int(d) for d in match.group() if d.isdigit()]
    if len(digits) < 13:
        return match.group()
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    if total % 10 == 0:
        return "[CREDIT_CARD]"
    return match.group()


# Structured PII patterns (compiled once at module level)
_PII_PATTERNS = (
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"\b(?!000|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"), "[SSN]"),
    (
        re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
        "[PHONE]",
    ),
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "[IP]"),
    (re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b"), "[IP]"),
    # US street addresses: "123 Main Street", "456 Oak Ave"
    (
        re.compile(
            r"\b\d{1,6}\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+"
            r"(?:Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Road|Rd|"
            r"Lane|Ln|Way|Court|Ct|Place|Pl|Circle|Cir|Trail|Trl|"
            r"Parkway|Pkwy|Highway|Hwy)\b\.?",
            re.IGNORECASE,
        ),
        "[ADDRESS]",
    ),
    # US ZIP codes: 62701, 62701-1234
    # (only match when near state abbreviations or standalone)
    (
        re.compile(
            r"\b(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|"
            r"MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|"
            r"TN|TX|UT|VT|VA|WA|WV|WI|WY)\s+\d{5}(?:-\d{4})?\b"
        ),
        "[LOCATION]",
    ),
)


class RegexScrubber:
    """Fallback regex-only scrubber for structured PII.

    Does NOT detect names or addresses — only patterns:
    emails, phone numbers, credit cards, SSNs, IPs.
    """

    has_ner = False

    def __init__(self):
        logger.info("Regex-only scrubber initialized (no name detection)")

    def scrub(self, text: str) -> str:
        """Scrub structured PII patterns and secrets from text."""
        # Unicode normalization first (defeats homoglyph/zero-width evasion)
        text = normalize_text(text)
        result = scrub_urls(text)
        result = scrub_secrets(result)
        # Credit cards use Luhn checksum to avoid false positives
        result = _CC_RE.sub(_luhn_check, result)
        for pattern, replacement in _PII_PATTERNS:
            result = pattern.sub(replacement, result)
        return result


def create_scrubber(use_presidio: bool = True) -> Scrubber:
    """Create the appropriate scrubber based on available dependencies."""
    if use_presidio:
        try:
            return PresidioScrubber()
        except ImportError:
            logger.warning(
                "Presidio not available, falling back to"
                " regex-only scrubber. Install with:"
                " uv pip install presidio-analyzer"
                " presidio-anonymizer spacy"
                " && python -m spacy download en_core_web_lg"
            )
    return RegexScrubber()
