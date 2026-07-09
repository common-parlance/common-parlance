"""PII scrubbing pipeline.

Uses Presidio for NER-based detection (names and locations)
and regex-based detection (emails, phones, credit cards, SSNs, IPs).
Replaces detected PII with typed placeholders: [NAME_1], [EMAIL], etc.

Limitations (be honest about these — they shape the trust model):
  * NER name/location detection is English-only (en_core_web_lg) and
    probabilistic. Thresholds are tuned for precision over recall, so some
    names will be missed. This layer is a net, not a guarantee.
  * Secret detection is best-effort: known vendor prefixes + entropy-gated
    token/base64 heuristics. It is NOT a replacement for a dedicated
    scanner (TruffleHog / detect-secrets / gitleaks), which should run as a
    backstop before publication.
  * Because no automated pass is complete, the pipeline assumes a mandatory
    human review of the redaction diff before anything leaves the machine.
"""

import logging
import math
import re
import unicodedata
from collections import Counter
from typing import Protocol

from common_parlance._stopwords import STOPWORDS

logger = logging.getLogger(__name__)

# --- Unicode normalization (adversarial PII evasion defense) ---
# Zero-width characters, bidi overrides, and Unicode compatibility variants
# (fullwidth/mathematical/ligature forms) can bypass both regex and NER.
# NFKC normalization folds those compatibility variants to canonical ASCII
# and we strip the invisible control characters. NOTE: NFKC does NOT map
# cross-script homoglyphs (e.g. Cyrillic а U+0430 stays distinct from Latin
# a) — defeating those requires Unicode TR39 confusables mapping, which is a
# known gap (see test_normalize_cyrillic_homoglyphs_not_mapped). Must run
# before any pattern matching or NER analysis.
# Reference: "Unmasking the Reality of PII Masking Models" (arXiv:2504.12308)

# Invisible/control characters that can break tokenization without
# changing visible text. Covers zero-width spaces, joiners, bidi
# overrides, and other Unicode format characters.
_INVISIBLE_RE = re.compile(
    "["
    "\u061c"  # arabic letter mark (bidi control)
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
    "\u2066"  # left-to-right isolate
    "\u2067"  # right-to-left isolate
    "\u2068"  # first strong isolate
    "\u2069"  # pop directional isolate
    "\ufeff"  # byte order mark / zero-width no-break space
    "\ufff9"  # interlinear annotation anchor
    "\ufffa"  # interlinear annotation separator
    "\ufffb"  # interlinear annotation terminator
    "]+",
)


def normalize_text(text: str) -> str:
    """Normalize text to defeat adversarial PII evasion.

    1. NFKC normalization — folds Unicode compatibility variants to their
       canonical forms (fullwidth Ａ → A, mathematical 𝐀 → A, ligatures).
       It does NOT map cross-script homoglyphs (Cyrillic а stays Cyrillic);
       that needs TR39 confusables mapping (a known gap).
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
    re.compile(r"\b(sk-[a-zA-Z0-9_\-]{20,})\b"),  # OpenAI (incl. sk-proj-)
    re.compile(r"\b(sk-ant-[a-zA-Z0-9\-]{20,})\b"),  # Anthropic
    re.compile(r"\b(ghp_[a-zA-Z0-9]{36,})\b"),  # GitHub PAT (classic)
    re.compile(r"\b(gho_[a-zA-Z0-9]{36,})\b"),  # GitHub OAuth
    re.compile(r"\b(ghu_[a-zA-Z0-9]{36,})\b"),  # GitHub user-to-server
    re.compile(r"\b(ghs_[a-zA-Z0-9]{36,})\b"),  # GitHub server-to-server
    re.compile(r"\b(ghr_[a-zA-Z0-9]{36,})\b"),  # GitHub refresh
    re.compile(r"\b(github_pat_[a-zA-Z0-9_]{60,})\b"),  # GitHub fine-grained PAT
    re.compile(r"\b(glpat-[a-zA-Z0-9\-_]{20,})\b"),  # GitLab PAT
    re.compile(r"\b(xoxb-[a-zA-Z0-9\-]{20,})\b"),  # Slack bot
    re.compile(r"\b(xoxp-[a-zA-Z0-9\-]{20,})\b"),  # Slack user
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),  # AWS access key
    re.compile(r"\b(hf_[a-zA-Z0-9]{20,})\b"),  # HuggingFace
    re.compile(r"\b(npm_[a-zA-Z0-9]{36,})\b"),  # npm
    re.compile(r"\b(pypi-[a-zA-Z0-9]{20,})\b"),  # PyPI
    re.compile(r"\b(AIza[a-zA-Z0-9\-_]{35})\b"),  # Google API key
    re.compile(r"\b(ya29\.[a-zA-Z0-9\-_]{20,})\b"),  # Google OAuth access token
    re.compile(r"\b(AC[a-f0-9]{32})\b"),  # Twilio Account SID
    re.compile(r"\b(SK[a-f0-9]{32})\b"),  # Twilio API key SID
    re.compile(r"\b(sk_live_[a-zA-Z0-9]{24,})\b"),  # Stripe secret
    re.compile(r"\b(pk_live_[a-zA-Z0-9]{24,})\b"),  # Stripe public
    re.compile(r"\b(rk_live_[a-zA-Z0-9]{24,})\b"),  # Stripe restricted
    re.compile(r"\b(SG\.[a-zA-Z0-9\-_]{22,})\b"),  # SendGrid
    re.compile(r"\b(dop_v1_[a-zA-Z0-9]{64})\b"),  # DigitalOcean
    re.compile(r"\b(eyJ[a-zA-Z0-9\-_]{20,}\.eyJ[a-zA-Z0-9\-_]{20,})\b"),  # JWT
]

# Multi-line secret patterns (private keys, connection strings)
_SECRET_BLOCK_PATTERNS = [
    # Full PEM private-key BLOCK (header..footer) so the key body is redacted,
    # not just the banner — an OpenSSH/RSA body's low-entropy base64 slips the
    # entropy pass otherwise. Non-greedy; header-only fallbacks below still
    # catch a truncated/pasted header with no footer.
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    ),
    re.compile(
        r"-----BEGIN PGP PRIVATE KEY BLOCK-----[\s\S]*?"
        r"-----END PGP PRIVATE KEY BLOCK-----"
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),  # truncated header
    re.compile(r"-----BEGIN PGP PRIVATE KEY BLOCK-----"),
    # Any scheme://user:pass@host carrying credentials (not just DB/broker
    # schemes), case-insensitive so FTP://, SMTP://, LDAP:// etc. are caught
    # (the bare [a-z] scheme leaked any uppercase/mixed-case scheme). The
    # password class excludes only whitespace/@ (NOT '/'): real passwords often
    # contain '/' (e.g. base64/URL-encoded secrets) and the old [^\s@/]+ left
    # those credentials un-redacted. Both the password ({1,256}) and scheme
    # ({0,40}) are length-bounded: allowing '/' in an UNbounded password made
    # "a://a://a://…" backtrack O(n^2) (each '://' start rescans the rest for a
    # missing '@'); the bound caps that rescan to a constant, keeping it linear.
    # No real credential is longer than 256 chars.
    re.compile(
        r"\b[a-z][a-z0-9+.\-]{0,40}://[^\s:@/]+:[^\s@]{1,256}@[^\s]+", re.IGNORECASE
    ),
    re.compile(r"Authorization:\s*Bearer\s+[a-zA-Z0-9\-_.]+", re.IGNORECASE),
    # Azure Storage / Service Bus connection-string secrets (no stable
    # prefix, so match the assignment + base64 value directly)
    re.compile(r"AccountKey=[A-Za-z0-9+/]{40,}={0,2}", re.IGNORECASE),
    re.compile(r"SharedAccessKey=[A-Za-z0-9+/]{20,}={0,2}", re.IGNORECASE),
    # Contextual secret assignments: a secret-y key name set to a 16+ char
    # value. Catches keyed secrets with no vendor prefix (e.g.
    # TWILIO_AUTH_TOKEN=<32 hex>) without blanket-redacting all hex. The
    # [:=] requirement avoids prose matches; a long *value* assigned to such
    # a name may over-redact, with review as the backstop.
    re.compile(
        r"(?i)[A-Za-z0-9_]{0,40}"  # bounded prefix (was *) — avoids O(n^2) backtracking
        r"(?:auth[_-]?token|api[_-]?key|access[_-]?token|client[_-]?secret"
        r"|secret[_-]?key|password|passwd|secret)"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9+/._-]{16,}"
    ),
    # GCP service-account key fingerprint. The private_key (PEM) is caught
    # above and client_email by the EMAIL pass; this redacts the 40-hex
    # private_key_id those miss. project_id/client_id are deliberately left as
    # quasi-identifiers (review backstop), not blanket-redacted.
    re.compile(r'(?i)"?private[_-]?key[_-]?id"?\s*[:=]\s*"?[0-9a-f]{32,}'),
    # Presigned-URL signatures (AWS SigV4 X-Amz-Signature, GCS V4
    # X-Goog-Signature, Azure SAS sig=). On the client these are normally
    # stripped by scrub_urls (which runs first and reduces the whole URL to
    # [URL:host]); this backstops a bare signature param outside a scheme:// URL
    # and keeps the client in parity with the Worker gate, which ignores URLs and
    # whose entropy backstop excuses URL-shaped tokens — so a bypassing client
    # could otherwise publish a live signed URL. The param name is a specific
    # literal, so the value class is permissive; bounded {N,M} keeps it linear.
    # Value bound {16,512}: an AWS SigV4 signature is 64 hex, but a GCS V4
    # RSA-SHA256 signature is 512 hex (2048-bit key). A tighter bound truncates
    # the match and leaves the tail in the clear on a bare param — under-redaction.
    re.compile(r"(?i)X-(?:Amz|Goog)-Signature=[0-9a-fA-F]{16,512}"),
    re.compile(r"(?i)[?&]sig=[A-Za-z0-9%+/=]{40,512}"),
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

# Standalone base64 blobs (Azure keys, encoded credentials, etc.) that the
# generic token pattern misses because of +/= characters. Word boundaries
# don't work around +/=, so isolate the blob with explicit non-base64
# lookaround. Threshold of 28 chars (~21 bytes) catches medium-length keys
# the 24-char generic pattern fragments on; entropy-gated to avoid redacting
# ordinary prose or short identifiers.
_BASE64_BLOB_RE = re.compile(
    r"(?<![A-Za-z0-9+/])([A-Za-z0-9+/]{28,}={0,2})(?![A-Za-z0-9+/])"
)

# Dotted/structured encoded tokens (e.g. "id.base64payload", JWT-like) that the
# plain base64 pass splits on '.'. The charset includes '.', so we gate
# redaction on the token ALSO carrying a base64 payload char (+ or /) — that
# distinguishes encoded secrets from plain dotted identifiers (java.package
# names, version strings, config keys), which have no + or /.
_DOTTED_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9+/])([A-Za-z0-9][A-Za-z0-9+/._-]{26,}={0,2})(?![A-Za-z0-9+/=._-])"
)


def _shannon_entropy(s: str) -> float:
    """Calculate Shannon entropy of a string in bits per character."""
    if not s:
        return 0.0
    length = len(s)
    return -sum((c / length) * math.log2(c / length) for c in Counter(s).values())


# --- Structural false-positive rejectors (precision) ---
# Real code is full of high-entropy-LOOKING but benign strings: schemeless URLs
# / paths and long structured identifiers (CamelCase, snake_case). Entropy can't
# tell these from secrets (character diversity != randomness), so reject them
# structurally before redacting — the approach gitleaks (stopwords), detect-
# secrets (gibberish/wordlist filters), and GitGuardian (HeuristicPostValidator)
# converge on. Refs: SecretBench has_words/in_url (arXiv:2303.06729); patent
# US10878088B2 (case transitions are structure, not randomness). Applied only to
# the context-free entropy passes; vendor-prefix and keyed (AUTH_TOKEN=) passes
# still catch prefixed and word-like secrets.

# URL / schemeless-domain / path shape — the WHOLE token (fullmatch, not search):
# a scheme URL, or a TLD-bearing host with an optional /-path. Anchored so a
# secret merely *containing* a "host.tld/" fragment is not excused. Real URLs/
# paths carry no base64 '+'/'='.
_URLISH_RE = re.compile(
    r"[a-z][a-z0-9+.-]{0,40}://\S+|(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:/[A-Za-z0-9._~%-]*)*"
)

# CamelCase / lower / UPPER alphabetic runs (segment a structured identifier).
_IDENT_SEG_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+")


# A non-dictionary alphabetic run longer than this is secret-shaped, not part
# of a real identifier — see _looks_benign. (Long real acronyms/segments such as
# HTTPS, XMLHTTP stay well under it.)
_MAX_NONWORD_RUN = 12

# Cap the substring length we run entropy on. shannon_entropy is O(n), so a
# multi-KB token could burn CPU; but SKIPPING long tokens entirely let an inline
# secret blob > this size bypass the entropy backstop. We evaluate a bounded
# PREFIX instead — a high-entropy secret's prefix is still high-entropy — and
# redact the whole token on a hit. Mirrors the JS MAX_ENTROPY_TOKEN.
_MAX_ENTROPY_TOKEN = 4096


def _ident_segments(token: str) -> list[str]:
    """All CamelCase / case-run alphabetic segments of a token.

    A structured identifier like ``WrappedResourceManager`` splits into
    wrapped/resource/manager — all dictionary words; a secret-shaped token like
    ``wJalrXUtnFEMIK7MDENGbPxRfiCYEX`` splits into runs that are not words. A
    vowel/length heuristic can't tell the two apart (a random base64 run is full
    of vowels too) — only a wordlist can, which is why gitleaks and
    detect-secrets gate on one.
    """
    segs: list[str] = []
    for part in re.split(r"[^A-Za-z]+", token):
        segs += _IDENT_SEG_RE.findall(part)
    return segs


def _looks_benign(token: str) -> bool:
    """True if a high-entropy token is structurally a benign URL/path, or a
    word-structured identifier (>=2 dictionary words covering >=half the token),
    rather than a secret. Precision rejector for the context-free entropy passes.

    STOPWORDS holds only lowercase entries of length >= 4, so short connector
    runs (id, by) never match; coverage carries them.
    """
    if _URLISH_RE.fullmatch(token):
        return True
    segs = _ident_segments(token)
    words = [s for s in segs if s.lower() in STOPWORDS]
    if len(words) < 2:
        return False
    # A long non-dictionary run means a secret tail padded with real words
    # ("ConfigurationManagerZMDENGBPXRFICYEX") — don't excuse it just because
    # the words cover half the length.
    if any(len(s) > _MAX_NONWORD_RUN for s in segs if s.lower() not in STOPWORDS):
        return False
    return sum(len(w) for w in words) / len(token) >= 0.5


def _check_entropy(match: re.Match) -> str:
    """Replace high-entropy tokens with [SECRET]."""
    token = match.group(1)
    # Evaluate a bounded prefix (cost guard) rather than skipping long blobs —
    # skipping let an inline secret > _MAX_ENTROPY_TOKEN bypass detection.
    sample = token[:_MAX_ENTROPY_TOKEN]
    if _looks_benign(sample):
        return token
    # Coding corpora contain many long CamelCase identifiers with high
    # character diversity. Require at least one digit before treating a
    # generic token as secret-like; vendor/keyed/base64 paths still catch
    # common pure-letter secrets with stronger context.
    if not any(ch.isdigit() for ch in sample):
        return token
    if _shannon_entropy(sample) > 4.0:
        return "[SECRET]"
    return token


def _check_base64(match: re.Match) -> str:
    """Replace high-entropy base64 blobs with [SECRET]."""
    token = match.group(1)
    # Evaluate a bounded prefix (cost guard) rather than skipping long blobs.
    sample = token[:_MAX_ENTROPY_TOKEN]
    if _looks_benign(sample):
        return token
    # Long pure-letter identifiers can look high-entropy under the base64
    # charset. Require either a digit or a base64-only/padding character before
    # redacting this generic fallback.
    if not any(ch.isdigit() or ch in "+/=" for ch in sample):
        return token
    # 40-char hex (e.g. git SHAs) sits around 4.0; require strictly higher
    # so commit hashes and similar non-secrets are left intact.
    if _shannon_entropy(sample) > 4.0:
        return "[SECRET]"
    return token


def _check_dotted_token(match: re.Match) -> str:
    """Replace dotted high-entropy encoded tokens (with + or /) with [SECRET].

    Requires both a '.' (the char that splits the plain base64 pass) and a
    base64 payload char (+ or /), so plain dotted identifiers like
    java.package.names, version strings, and config keys are left alone.
    """
    token = match.group(1)
    # Evaluate a bounded prefix (cost guard) rather than skipping long blobs.
    sample = token[:_MAX_ENTROPY_TOKEN]
    if _looks_benign(sample):
        return token
    if "." not in sample or ("+" not in sample and "/" not in sample):
        return token
    if _shannon_entropy(sample) > 4.0:
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

    # Pass 1d: standalone base64 blobs (entropy-gated). Runs before the
    # generic token pass so blobs containing +/= are replaced whole rather
    # than fragmented.
    result = _BASE64_BLOB_RE.sub(_check_base64, result)

    # Pass 1e: dotted encoded tokens the base64 pass splits on '.'
    result = _DOTTED_TOKEN_RE.sub(_check_dotted_token, result)

    # Pass 2: generic high-entropy tokens (conservative threshold)
    # Only flag strings with entropy > 4.0 bits/char (random alphanumeric
    # averages ~5.2; English text averages ~3.5-4.0)
    result = _GENERIC_TOKEN_RE.sub(_check_entropy, result)
    return result


# --- URL reduction ---
# Preserve domain context (useful for technical discussions) but strip full
# paths, query strings, and credentials to prevent SEO/spam abuse and the leak
# of private filenames/repo names/tokens that live in URL paths. Covers the
# path-leaking web/file/transfer schemes — not just http(s): ftp/git/ws/s3/sftp
# carry the same sensitive path data, and the PresidioScrubber no longer
# recognizes URLs, so this regex is the sole URL reducer. The scheme list
# DELIBERATELY excludes database/connection-string schemes (postgres, redis,
# mysql, mongodb, amqp, …): those are owned by the connection-string secret
# pattern (creds redacted; credential-less service URLs preserved as technical
# context), and reducing them here would over-redact and break that contract.
_URL_RE = re.compile(
    r"(?:https?|ftps?|sftp|ssh|git|wss?|s3|gs)://(?:[^\s/@]+@)?([^\s/:?#]+)[^\s]*",
    re.IGNORECASE,
)
# Known scammy/spam TLDs — flag URLs on these as [URL:suspicious]. Curated from
# current abuse data (Spamhaus "most abused TLDs", Interisle Cybercrime Supply
# Chain) rather than a stale block. Deliberately conservative: a TLD earns a
# spot only if it is overwhelmingly abuse-dominated, because the cost of a wrong
# entry is silently rewriting a legitimate link in published data.
#   - Dropped: xyz/work/stream/download/racing (now host substantial legit use),
#     hair/beauty/rest (never meaningfully abused), and the ex-Freenom
#     gq/ml/cf/tk/ga (free registration ended in 2023; abuse collapsed with it).
#   - Kept: top/click/icu/win/bid/loan/monster/sbs/cfd/buzz.
#   - Added: bond/cyou/autos/xin (abuse-skewed). NOT shop/vip — both are
#     brandable gTLDs with very large legitimate use (e.g. *.shop storefronts),
#     so flagging them would silently rewrite real links, violating the rule
#     above; their abuse *rate*, not raw count, is what disqualifies them.
_SPAM_TLDS = frozenset(
    {
        "top",
        "click",
        "icu",
        "win",
        "bid",
        "loan",
        "monster",
        "sbs",
        "cfd",
        "buzz",
        "bond",
        "cyou",
        "autos",
        "xin",
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
            # URL is intentionally NOT mapped. URLs are already reduced to
            # [URL:domain] by the regex scrub_urls() pass that runs BEFORE
            # Presidio. If Presidio also recognized URLs, its URL recognizer
            # would re-match the bare domain inside "[URL:example.com]" and
            # rewrite it to [URL], yielding a malformed "[URL:[URL]]" with the
            # domain discarded. Bare (scheme-less) domains are preserved by
            # design (domain context); review catches genuinely sensitive ones.
            "IBAN_CODE": "IBAN",
            "NRP": "GROUP",
            "MEDICAL_LICENSE": "MEDICAL_ID",
            "US_DRIVER_LICENSE": "DRIVER_LICENSE",
            "DATE_TIME": "DATE",
        }

        # Static replacements for every type EXCEPT PERSON. PERSON gets a
        # fresh per-call numbered operator so repeated names become
        # consistent [NAME_1]/[NAME_2] placeholders (coreference preservation
        # — see docs/design.md and DEVELOPMENT.md). OperatorConfig is kept as
        # an instance attr so the helper can build operators without
        # re-importing presidio (which stays an optional dependency).
        self._OperatorConfig = OperatorConfig
        self._static_operators = {
            entity_type: OperatorConfig("replace", {"new_value": f"[{placeholder}]"})
            for entity_type, placeholder in self._type_map.items()
            if entity_type != "PERSON"
        }
        # Only analyze for types we explicitly map. This deliberately excludes
        # ORGANIZATION: spaCy ORG NER is noisy (misclassifies products like
        # "Stripe"/"Postgres"), org/product names are high-utility in coding
        # data and rarely the sensitive identifier, and leaving it unmapped
        # otherwise leaks an ugly "<ORGANIZATION>" default placeholder.
        # Genuinely sensitive internal org names are caught at review.
        self._entities = list(self._type_map.keys())

        # Presidio's allow_list is case-sensitive, so include both
        # original casing and lowercase to catch "Git" and "git".
        self._allow_list = list(
            {t for term in _PROGRAMMING_ALLOW_LIST for t in (term, term.lower())}
        )

        logger.info("Presidio scrubber initialized with model: %s", model_name)

    def _numbered_person_operator(self, results: list, text: str):
        """Build a custom operator mapping each distinct name to a stable
        [NAME_n] placeholder, numbered in reading order, so coreference is
        preserved (the same person reads as the same placeholder).
        """
        mapping: dict[str, str] = {}
        persons = sorted(
            (r for r in results if r.entity_type == "PERSON"),
            key=lambda r: r.start,
        )
        for r in persons:
            key = " ".join(text[r.start : r.end].split()).casefold()
            if key not in mapping:
                mapping[key] = f"[NAME_{len(mapping) + 1}]"

        def _replace(value: str) -> str:
            key = " ".join(value.split()).casefold()
            placeholder = mapping.get(key)
            if placeholder is None:  # offset/normalization edge case
                placeholder = f"[NAME_{len(mapping) + 1}]"
                mapping[key] = placeholder
            return placeholder

        return self._OperatorConfig("custom", {"lambda": _replace})

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
            entities=self._entities,
            score_threshold=_DEFAULT_SCORE_THRESHOLD,
            allow_list=self._allow_list,
        )

        # Apply stricter per-entity thresholds
        results = self._filter_results(results, text)

        if not results:
            return text

        operators = {
            **self._static_operators,
            "PERSON": self._numbered_person_operator(results, text),
        }
        anonymized = self._anonymizer.anonymize(
            text=text,
            analyzer_results=results,
            operators=operators,
        )

        # Run regex patterns as safety net (catches what NER misses)
        result = anonymized.text
        result = _CC_RE.sub(_luhn_check, result)
        result = _IPV6_RE.sub(_check_ipv6, result)
        for pattern, replacement in _PII_PATTERNS:
            result = pattern.sub(replacement, result)
        return result


# Each digit may be followed by at most ONE space/dash separator. The previous
# lazy `[ -]*?` made `{13,19}` backtrack catastrophically on inputs like
# "4-4-4-…" (quadratic — a 100KB run burned seconds of CPU); a single greedy
# optional separator matches real card formatting ("4111 1111 1111 1111",
# "4111-1111-…") and runs in linear time.
_CC_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


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


# IPv6 — full AND compressed (`::`) forms. The old pattern required all 8
# hextets, so `::1` / `2001:db8::1` / `fe80::1` (the common real-world forms)
# leaked. Bounded {1,n} quantifiers → no catastrophic backtracking.
_IPV6_RE = re.compile(
    r"(?<![:.\w])(?:"
    r"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}|"
    r"(?:[0-9A-Fa-f]{1,4}:){1,7}:|"
    r"(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|"
    r"(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}|"
    r"(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}|"
    r"(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}|"
    r"(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}|"
    r"[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}|"
    r":(?:(?::[0-9A-Fa-f]{1,4}){1,7}|:)"
    r")(?![:.\w])"
)


def _check_ipv6(match: re.Match) -> str:
    """Redact an IPv6 address, but only if it contains a digit. All-hex-letter
    `::` tokens (e.g. C++/Rust scope like `dead::beef`) are far more likely code
    than an address; requiring a digit avoids eating those identifiers. Real
    IPv6 addresses almost always carry digits.
    """
    return "[IP]" if any(c.isdigit() for c in match.group(0)) else match.group(0)


# Structured PII patterns (compiled once at module level)
_PII_PATTERNS = (
    # Local/domain parts are length-bounded to RFC 5321 limits (64 / 255). The
    # unbounded `+` made this backtrack catastrophically (O(n^2)) on a long run
    # of local-part-class chars with no '@' (e.g. "4-4-4-…") — a ReDoS. Bounding
    # matches every real address and runs in linear time.
    (
        re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,}\b"),
        "[EMAIL]",
    ),
    (re.compile(r"\b(?!000|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"), "[SSN]"),
    (
        re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
        "[PHONE]",
    ),
    # International E.164-style: +<country code> then 7–14 more digits with
    # separators. The leading '+' anchors it (0.2% FP on real code; ReDoS-safe,
    # bounded {6,13}). Catches +CC formats the US NANP pattern misses (validated
    # 0%→ caught by seeded recall); leading-0 national / parenthesized formats are
    # deliberately not chased (high FP, outside the disclosed US scope). See
    # benchmark/RESULTS.md.
    (re.compile(r"\+\d{1,3}[-.\s]?\d(?:[-.\s]?\d){6,13}"), "[PHONE]"),
    # IPv4 with octet-range validation (each part 0-255). The leading `0{0,2}`
    # accepts zero-padded octets (e.g. 192.168.001.001 / 010.0.0.1, common in
    # firewall/router and Windows logs) which the bare 0-255 alternation
    # rejected — those raw IPs were leaking. Left boundary (?<![\d.]) stops a
    # 4-octet window inside a longer dotted string from the left; right boundary
    # (?!\.?\d) rejects a 5th octet (".5") but ALLOWS a trailing sentence period
    # ("8.8.8.8." must still redact) — a plain (?![\d.]) leaked an IP at end of
    # sentence. So 1.0.2403.1 (octet > 255) and 100.200.50.25.300 (5 parts) are
    # left intact while real IPs still match.
    (
        re.compile(
            r"(?<![\d.])(?:0{0,2}(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9]))"
            r"(?:\.(?:0{0,2}(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9]))){3}(?!\.?\d)"
        ),
        "[IP]",
    ),
    # IPv6 (full + compressed) handled by _IPV6_RE/_check_ipv6 below, not here.
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
        result = _IPV6_RE.sub(_check_ipv6, result)
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
