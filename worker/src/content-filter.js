/**
 * Content filtering and PII validation.
 *
 * Server-side defense-in-depth checks that run before accepting uploads.
 * Mirrors client-side patterns in scrub.py and filter.py.
 */

import { STOPWORDS } from "./stopwords.js";

// --- Content filter (server-side, mirrors client blocklist) ---
// Keep in sync with blocklists/*.txt in the Python package.

export const BLOCKLIST_PATTERNS = [
  // CSAM indicators. \s* (not \s+) between keywords: the separator-collapse in
  // normalizeLeet turns "child.porn" into "childporn" (no space), and a bare
  // "childporn" has none either, so \s+ missed both. \s* matches the spaced,
  // separated, and concatenated forms. Keep in sync with csam_indicator.txt.
  {
    pattern: /child\s*(?:porn|sex|abuse|exploit|erotic|nude)/i,
    category: "csam_indicator",
  },
  {
    pattern: /minor\s*(?:porn|sex|abuse|exploit|erotic|nude)/i,
    category: "csam_indicator",
  },
  {
    pattern: /underage\s*(?:porn|sex|abuse|exploit|erotic|nude)/i,
    category: "csam_indicator",
  },
  {
    pattern:
      /(?:porn|sex|erotic|nude)\s*(?:child|minor|underage|preteen|prepubescent)/i,
    category: "csam_indicator",
  },
  { pattern: /pedophil[ei]/i, category: "csam_indicator" },
  {
    pattern: /preteen\s*(?:sex|nude|erotic|naked)/i,
    category: "csam_indicator",
  },
  {
    pattern:
      /(?:sex|erotic|naked|nude)\s*(?:boy|girl)\s*(?:young|little|small)/i,
    category: "csam_indicator",
  },

  // Dangerous instructions
  {
    pattern:
      /how\s+to\s+(?:make|build|create|synthesize)\s+(?:an?\s+)?(?:bomb|explosive|nerve\s+agent|sarin|anthrax|ricin|mustard\s+gas)/i,
    category: "dangerous_instructions",
  },
  {
    pattern:
      /(?:synthesize|manufacture)\s+(?:fentanyl|methamphetamine|sarin|vx\s+gas)/i,
    category: "dangerous_instructions",
  },
];

// Leetspeak normalization (mirrors client-side _normalize_leet in filter.py)
const LEET_MAP = {
  "@": "a",
  "4": "a",
  "8": "b",
  "(": "c",
  "3": "e",
  "1": "i",
  "!": "i",
  "|": "l",
  "0": "o",
  $: "s",
  "5": "s",
  "7": "t",
  "+": "t",
};

function normalizeLeet(text) {
  // Collapse separator characters between alphanumeric chars
  let collapsed = text.replace(
    /(?<=[a-zA-Z0-9@$!|+])[*._\-]{1,3}(?=[a-zA-Z0-9@$!|+])/g,
    ""
  );
  // Apply character substitutions by iterating LEET_MAP directly, so the
  // substitution can't silently omit a map key (a prior char-class literal
  // dropped "1"->"i", letting "ch1ld ..." bypass the server filter while the
  // client's str.translate caught it). Mirrors filter.py's translate().
  let out = "";
  for (const ch of collapsed) out += LEET_MAP[ch] || ch;
  return out;
}

// Collapse runs of single characters separated by 1-3 spaces/punctuation
// ("c h i l d p o r n" -> "childporn"). Only *runs of single chars* are
// collapsed: whole-word spacing ("child porn") is already caught by the \s* in
// the patterns and must NOT be touched here, because blanket whitespace removal
// would fuse normal prose ("child sex education" -> "childsexeducation") and
// mass-false-block legitimate text. The boundary lookarounds force every char
// in the run to be single; the bounded {1,3}/{2,} quantifiers keep it linear
// (no catastrophic backtracking — same ReDoS guard as the other gate patterns).
// Mirrors filter.py _collapse_spaced() byte-for-byte.
const SPACED_RUN_RE =
  /(?<![A-Za-z0-9])[A-Za-z0-9](?:[\s*._\-]{1,3}[A-Za-z0-9]){2,}(?![A-Za-z0-9])/g;
function collapseSpaced(text) {
  return text.replace(SPACED_RUN_RE, (m) => m.replace(/[\s*._\-]+/g, ""));
}

// Drop Unicode combining marks (Zalgo / stacked-diacritic evasion). NFKC (in
// normalizeUnicode) does NOT remove them, so "çḥíl̀d" survives the rest of the
// pipeline. NFD-decompose and strip every mark (\p{M}) so the base letters
// re-form the word for matching. Mirrors filter.py _strip_combining().
function stripMarks(text) {
  return text.normalize("NFD").replace(/\p{M}/gu, "");
}

// --- Unicode normalization (adversarial PII evasion defense) ---
// Zero-width chars, bidi overrides, and Unicode compatibility variants
// (fullwidth/mathematical/ligature) can bypass regex and NER. NFKC folds
// those compatibility variants to canonical ASCII and we strip invisibles.
// NOTE: NFKC does NOT map cross-script homoglyphs (Cyrillic а stays distinct
// from Latin a) — that needs TR39 confusables mapping (known gap). Must run
// before any pattern matching or content checks.

// Invisible/control characters that break tokenization without changing
// visible text: zero-width spaces/joiners, bidi overrides, etc.
const INVISIBLE_RE =
  /[\u061c\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2061\u2062\u2063\u2064\u2066\u2067\u2068\u2069\ufeff\ufff9\ufffa\ufffb]+/g;

function normalizeUnicode(text) {
  // NFKC folds Unicode compatibility variants (fullwidth Ａ → A,
  // mathematical 𝐀 → A); cross-script homoglyphs are NOT mapped.
  let normalized = text.normalize("NFKC");
  // Strip invisible characters
  normalized = normalized.replace(INVISIBLE_RE, "");
  // Normalize the whitespace chars that Python's \s matches but JS's \s does
  // NOT (C0 separators U+001C–U+001F and NEL U+0085) to a plain space, so the
  // worker's \s-based gate logic (collapseSpaced, the blocklist \s*) matches
  // the client's. Without this a CSAM term spaced with these chars
  // ("c\x1ch\x1ci\x1cl\x1cd") collapsed and blocked on the client but PASSED
  // the worker — the final gate. (U+00A0, U+2028, U+2029 … are matched by both
  // engines' \s, so only this small divergent set needs folding.)
  normalized = normalized.replace(/[\u001c-\u001f\u0085]/g, " ");
  return normalized;
}

// Cross-script homoglyphs → ASCII (a curated TR39-style skeleton over the
// scripts used in real homoglyph evasion: Cyrillic + Greek). NFKC does NOT fold
// these, so the CSAM/dangerous-instructions blocklist would otherwise be
// bypassed by a single lookalike (e.g. Cyrillic о U+043E in "bоmb"). Applied
// ONLY in checkContent — NOT in checkPii, where homoglyph-folding is a
// deliberate wontfix (see scrub.py normalize_text + the corpus cyrillic gap).
const CONFUSABLES = {
  а: "a", е: "e", о: "o", р: "p", с: "c", у: "y", х: "x", к: "k", м: "m",
  т: "t", н: "h", в: "b", і: "i", ј: "j", ѕ: "s", ԁ: "d", А: "A", Е: "E",
  О: "O", Р: "P", С: "C", У: "Y", Х: "X", К: "K", М: "M", Т: "T", Н: "H",
  В: "B", І: "I",
  ο: "o", α: "a", ε: "e", ι: "i", ν: "v", ρ: "p", τ: "t", κ: "k", χ: "x",
  υ: "u", Ο: "O", Α: "A", Ε: "E", Ι: "I", Ν: "N", Ρ: "P", Τ: "T", Κ: "K",
  Χ: "X", Β: "B", Ζ: "Z", Η: "H", Μ: "M",
};

function foldConfusables(text) {
  let out = "";
  for (const ch of text) out += CONFUSABLES[ch] || ch;
  return out;
}

// --- Server-side PII validation (defense-in-depth) ---
// Keep in sync with scrub.py _SECRET_PREFIX_PATTERNS, _SECRET_BLOCK_PATTERNS,
// _FILE_PATH_PATTERNS, and _PII_PATTERNS. This is the trust boundary — if the
// client scrubber misses something (or is bypassed), these catch it.

const PII_PATTERNS = [
  // Structured PII (scrub.py _PII_PATTERNS)
  // Local/domain parts bounded to RFC 5321 limits (64 / 255). The unbounded `+`
  // backtracked catastrophically (O(n^2)) on a long local-part-class run with
  // no '@' (e.g. "4-4-4-…") — a ReDoS that hung the Worker for ~15s on a 100KB
  // body. Bounding matches every real address and runs in linear time.
  {
    pattern: /\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,}\b/,
    type: "email",
  },
  {
    pattern: /\b(?!000|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b/,
    type: "ssn",
  },
  {
    pattern: /\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b/,
    type: "phone",
  },
  // International E.164-style: +<country code> + 7–14 more digits with separators.
  // '+' anchor keeps FP low (0.2% on real code); bounded → linear. Mirrors
  // scrub.py; leading-0/parenthesized formats deliberately not chased (high FP).
  { pattern: /\+\d{1,3}[-.\s]?\d(?:[-.\s]?\d){6,13}/, type: "phone" },
  // IPv4 with octet-range validation (0-255). The leading 0{0,2} accepts
  // zero-padded octets (e.g. 192.168.001.001, common in firewall/router and
  // Windows logs) which the bare 0-255 alternation rejected. Left boundary
  // (?<![\d.]) and right boundary (?!\.?\d): the right one rejects a 5th octet
  // (".5") but ALLOWS a trailing sentence period ("8.8.8.8." must still match)
  // — a plain (?![\d.]) leaked an IP at end of sentence. Mirrors scrub.py.
  {
    pattern:
      /(?<![\d.])(?:0{0,2}(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9]))(?:\.(?:0{0,2}(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9]))){3}(?!\.?\d)/,
    type: "ip",
  },
  // IPv6 (full + compressed) handled by hasIpv6() in checkPii, not here.

  // File paths that leak usernames (scrub.py _FILE_PATH_PATTERNS)
  { pattern: /\/Users\/[a-zA-Z0-9_.-]+/, type: "filepath" },
  { pattern: /\/home\/[a-zA-Z0-9_.-]+/, type: "filepath" },
  { pattern: /C:\\Users\\[a-zA-Z0-9_.-]+/i, type: "filepath" },
  { pattern: /\/root\b/, type: "filepath" },

  // Known API key prefixes (scrub.py _SECRET_PREFIX_PATTERNS)
  { pattern: /\b(?:sk-[a-zA-Z0-9_\-]{20,})\b/, type: "api_key" },
  { pattern: /\b(?:sk-ant-[a-zA-Z0-9\-]{20,})\b/, type: "api_key" },
  { pattern: /\b(?:ghp_[a-zA-Z0-9]{36,})\b/, type: "api_key" },
  { pattern: /\b(?:gho_[a-zA-Z0-9]{36,})\b/, type: "api_key" },
  { pattern: /\b(?:ghu_[a-zA-Z0-9]{36,})\b/, type: "api_key" },
  { pattern: /\b(?:ghs_[a-zA-Z0-9]{36,})\b/, type: "api_key" },
  { pattern: /\b(?:ghr_[a-zA-Z0-9]{36,})\b/, type: "api_key" },
  { pattern: /\b(?:github_pat_[a-zA-Z0-9_]{60,})\b/, type: "api_key" },
  { pattern: /\b(?:glpat-[a-zA-Z0-9\-_]{20,})\b/, type: "api_key" },
  { pattern: /\b(?:xoxb-[a-zA-Z0-9\-]{20,})\b/, type: "api_key" },
  { pattern: /\b(?:xoxp-[a-zA-Z0-9\-]{20,})\b/, type: "api_key" },
  { pattern: /\b(?:AKIA[0-9A-Z]{16})\b/, type: "api_key" },
  { pattern: /\b(?:hf_[a-zA-Z0-9]{20,})\b/, type: "api_key" },
  { pattern: /\b(?:npm_[a-zA-Z0-9]{36,})\b/, type: "api_key" },
  { pattern: /\b(?:pypi-[a-zA-Z0-9]{20,})\b/, type: "api_key" },
  { pattern: /\b(?:AIza[a-zA-Z0-9\-_]{35})\b/, type: "api_key" },
  { pattern: /\b(?:ya29\.[a-zA-Z0-9\-_]{20,})\b/, type: "api_key" },
  { pattern: /\b(?:AC[a-f0-9]{32})\b/, type: "api_key" },
  { pattern: /\b(?:SK[a-f0-9]{32})\b/, type: "api_key" },
  { pattern: /\b(?:sk_live_[a-zA-Z0-9]{24,})\b/, type: "api_key" },
  { pattern: /\b(?:pk_live_[a-zA-Z0-9]{24,})\b/, type: "api_key" },
  { pattern: /\b(?:rk_live_[a-zA-Z0-9]{24,})\b/, type: "api_key" },
  { pattern: /\b(?:SG\.[a-zA-Z0-9\-_]{22,})\b/, type: "api_key" },
  { pattern: /\b(?:dop_v1_[a-zA-Z0-9]{64})\b/, type: "api_key" },
  {
    pattern: /\b(?:eyJ[a-zA-Z0-9\-_]{20,}\.eyJ[a-zA-Z0-9\-_]{20,})\b/,
    type: "jwt",
  },

  // Structural secret patterns (scrub.py _SECRET_BLOCK_PATTERNS)
  { pattern: /-----BEGIN [A-Z ]*PRIVATE KEY-----/, type: "private_key" }, // incl. plain PKCS#8
  { pattern: /-----BEGIN PGP PRIVATE KEY BLOCK-----/, type: "private_key" },
  // Case-insensitive (FTP://, SMTP://, LDAP:// …) and the password class
  // excludes only whitespace/@ (NOT '/') so base64/URL-encoded passwords aren't
  // leaked. Password is length-bounded ({1,256}): an unbounded '/'-allowing run
  // made "a://a://…" backtrack O(n^2), the bound keeps it linear. Mirrors
  // scrub.py.
  {
    pattern:
      /\b[a-z][a-z0-9+.-]{0,40}:\/\/[^\s:@/]+:[^\s@]{1,256}@[^\s]+/i,
    type: "connection_string",
  },
  {
    pattern: /Authorization:\s*Bearer\s+[a-zA-Z0-9\-_.]+/i,
    type: "bearer_token",
  },
  // Azure Storage / Service Bus connection-string secrets (no stable prefix)
  { pattern: /AccountKey=[A-Za-z0-9+/]{40,}={0,2}/i, type: "api_key" },
  { pattern: /SharedAccessKey=[A-Za-z0-9+/]{20,}={0,2}/i, type: "api_key" },
  // Contextual secret assignments (e.g. TWILIO_AUTH_TOKEN=<hex>): secret-y key
  // name set to a 16+ value. Catches keyed secrets with no vendor prefix
  // without blanket-redacting all hex.
  {
    pattern:
      /[A-Za-z0-9_]{0,40}(?:auth[_-]?token|api[_-]?key|access[_-]?token|client[_-]?secret|secret[_-]?key|password|passwd|secret)\s*[:=]\s*['"]?[A-Za-z0-9+/._-]{16,}/i,
    type: "api_key",
  },
  // GCP service-account key fingerprint. private_key (PEM) + client_email are
  // already caught above; this redacts the 40-hex private_key_id those miss.
  {
    pattern: /"?private[_-]?key[_-]?id"?\s*[:=]\s*"?[0-9a-f]{32,}/i,
    type: "gcp_key_id",
  },
  // Presigned-URL signatures (AWS SigV4, GCS V4, Azure SAS sig=). The Worker
  // ignores URLs and the entropy backstop excuses URL-shaped tokens, so without
  // these a bypassing client could publish a live signed URL whose signature is
  // a live secret. Specific param literal → permissive value class; bounded →
  // linear time. Mirrors scrub.py _SECRET_BLOCK_PATTERNS.
  {
    // {16,512}: AWS SigV4 is 64 hex, but GCS V4 RSA-SHA256 is 512 hex (2048-bit
    // key). A tighter bound truncates the match, leaving the tail in the clear.
    pattern: /X-(?:Amz|Goog)-Signature=[0-9a-fA-F]{16,512}/i,
    type: "url_signature",
  },
  { pattern: /[?&]sig=[A-Za-z0-9%+/=]{40,512}/i, type: "url_signature" },
];

// High-entropy base64 backstop — mirrors scrub.py _BASE64_BLOB_RE. Catches
// missed base64-ish secrets (e.g. Azure keys) the explicit prefixes don't.
// We fail closed on base64 blobs only: a blanket high-entropy reject on
// generic tokens would also reject benign git SHAs / lockfile integrity
// hashes and tank legitimate coding uploads. The proper backstop is a real
// scanner (TruffleHog / detect-secrets) — see roadmap.
const BASE64_BLOB_RE =
  /(?<![A-Za-z0-9+/])([A-Za-z0-9+/]{28,}={0,2})(?![A-Za-z0-9+/])/g;

// Dotted encoded tokens the plain base64 pass splits on '.'. Gated (in
// hasHighEntropySecret) on the token also carrying a + or / so plain dotted
// identifiers / version strings / config keys are left alone.
const DOTTED_TOKEN_RE =
  /(?<![A-Za-z0-9+/])([A-Za-z0-9][A-Za-z0-9+/._-]{26,}={0,2})(?![A-Za-z0-9+/=._-])/g;

function shannonEntropy(s) {
  if (!s) return 0;
  const counts = new Map();
  for (const ch of s) counts.set(ch, (counts.get(ch) || 0) + 1);
  let h = 0;
  for (const c of counts.values()) {
    const p = c / s.length;
    h -= p * Math.log2(p);
  }
  return h;
}

// --- Structural false-positive rejectors (precision) ---
// Real code is full of high-entropy-LOOKING but benign strings: schemeless URLs
// / paths and long structured identifiers. Entropy can't tell these from secrets
// (diversity != randomness), so reject them structurally. Mirrors scrub.py
// _looks_benign; refs: SecretBench has_words/in_url (arXiv:2303.06729), patent
// US10878088B2. Keep in sync with the Python passes.
// Anchored (^...$) URL/path shape for the WHOLE token; secrets carry +/= and
// won't fullmatch. Mirrors scrub.py _URLISH_RE.
const URLISH_RE =
  /^(?:[a-z][a-z0-9+.-]{0,40}:\/\/\S+|(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:\/[A-Za-z0-9._~%-]*)*)$/;
const IDENT_SEG_RE = /[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+/g;

// Alphabetic runs that are real dictionary words (in STOPWORDS, case-folded).
// A structured identifier (WrappedResourceManager) splits into dictionary
// words; a secret-shaped token (wJalrXUtnFEMIK7MDENGbPxRfiCYEX) does not. A
// vowel/length heuristic can't separate the two — only a wordlist can, which
// is why gitleaks/detect-secrets gate on one. Mirrors scrub.py
// _dictionary_segments. STOPWORDS holds only lowercase entries of length >= 4.
function identSegments(token) {
  const out = [];
  for (const part of token.split(/[^A-Za-z]+/)) {
    for (const seg of part.match(IDENT_SEG_RE) || []) {
      out.push(seg);
    }
  }
  return out;
}

// A non-dictionary alphabetic run longer than this is secret-shaped, not part
// of a real identifier (HTTPS/XMLHTTP stay well under it). Mirrors scrub.py.
const MAX_NONWORD_RUN = 12;

function looksBenign(token) {
  if (URLISH_RE.test(token)) return true;
  const segs = identSegments(token);
  const words = segs.filter((s) => STOPWORDS.has(s.toLowerCase()));
  if (words.length < 2) return false;
  // A long non-dictionary run means a secret tail padded with real words
  // ("ConfigurationManagerZMDENGBPXRFICYEX") — don't excuse it just because the
  // words cover half the length.
  if (segs.some((s) => s.length > MAX_NONWORD_RUN && !STOPWORDS.has(s.toLowerCase())))
    return false;
  const wordChars = words.reduce((a, s) => a + s.length, 0);
  // Divide by code-point length (not UTF-16 .length) so the ratio matches
  // Python's len(); keeps the benign decision identical across runtimes even
  // for astral chars. (Reachable tokens are ASCII today; this is insurance.)
  return wordChars / Array.from(token).length >= 0.5;
}

// shannonEntropy is O(n), so cap the substring we run it on to keep the CPU
// budget bounded. We evaluate a bounded PREFIX rather than SKIPPING long tokens
// — skipping let an inline secret blob larger than this bypass the entropy
// backstop. A high-entropy secret's prefix is still high-entropy. Mirrors
// scrub.py _MAX_ENTROPY_TOKEN.
const MAX_ENTROPY_TOKEN = 4096;

function hasHighEntropySecret(text) {
  BASE64_BLOB_RE.lastIndex = 0;
  let m;
  while ((m = BASE64_BLOB_RE.exec(text)) !== null) {
    const tok =
      m[1].length > MAX_ENTROPY_TOKEN ? m[1].slice(0, MAX_ENTROPY_TOKEN) : m[1];
    if (looksBenign(tok)) continue;
    if (/[0-9+/=]/.test(tok) && shannonEntropy(tok) > 4.0) {
      return true;
    }
  }
  DOTTED_TOKEN_RE.lastIndex = 0;
  while ((m = DOTTED_TOKEN_RE.exec(text)) !== null) {
    const tok =
      m[1].length > MAX_ENTROPY_TOKEN ? m[1].slice(0, MAX_ENTROPY_TOKEN) : m[1];
    if (looksBenign(tok)) continue;
    if (
      tok.includes(".") &&
      (tok.includes("+") || tok.includes("/")) &&
      shannonEntropy(tok) > 4.0
    ) {
      return true;
    }
  }
  return false;
}

// Credit cards — mirrors scrub.py _CC_RE + _luhn_check. A 13-19 digit run
// (at most one space/dash between digits) that passes the Luhn checksum. The
// client redacts these, so the trust boundary must reject them too; the Luhn
// gate keeps long non-card digit strings (order ids, hashes) from
// false-rejecting. A single greedy `[ -]?` (not lazy `[ -]*?`) is critical:
// the lazy form backtracked catastrophically on "4-4-4-…" (a 100KB body hung
// the Worker for ~minutes, blowing the CPU limit so the gate never completed).
const CC_RE = /\b(?:\d[ -]?){13,19}\b/g;

function luhnValid(digits) {
  let sum = 0;
  for (let i = 0; i < digits.length; i++) {
    let d = digits.charCodeAt(digits.length - 1 - i) - 48;
    if (i % 2 === 1) {
      d *= 2;
      if (d > 9) d -= 9;
    }
    sum += d;
  }
  return sum % 10 === 0;
}

function hasCreditCard(text) {
  CC_RE.lastIndex = 0;
  let m;
  while ((m = CC_RE.exec(text)) !== null) {
    const digits = m[0].replace(/\D/g, "");
    if (digits.length >= 13 && digits.length <= 19 && luhnValid(digits)) {
      return true;
    }
  }
  return false;
}

// IPv6 (full + compressed). Mirrors scrub.py _IPV6_RE/_check_ipv6: redact only
// addresses containing a digit, so all-hex-letter scope (`dead::beef`) isn't
// treated as an address. Bounded quantifiers → no catastrophic backtracking.
// Wrapped in (?<![:.\w]) ... (?![:.\w]) — the same boundaries scrub.py's
// _IPV6_RE uses — so an IPv6-shaped substring embedded in surrounding
// word/dot/colon characters ("deadbeef2001:db8::1") isn't matched, keeping the
// two runtimes' decisions identical.
const IPV6_RE =
  /(?<![:.\w])(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,7}:|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}|:(?:(?::[0-9A-Fa-f]{1,4}){1,7}|:))(?![:.\w])/g;

function hasIpv6(text) {
  IPV6_RE.lastIndex = 0;
  let m;
  while ((m = IPV6_RE.exec(text)) !== null) {
    if (/\d/.test(m[0])) return true;
  }
  return false;
}

// Placeholders that the client scrubber inserts. These must be stripped
// before checkPii() runs, or they'd false-positive match PII patterns.
// Keep in sync with scrub.py _type_map and placeholder strings.
const PII_ALLOWLIST = [
  "[EMAIL]",
  "[PHONE]",
  "[SSN]",
  "[IP]",
  "[PATH]",
  "[SECRET]",
  "[NAME]",
  "[LOCATION]",
  "[ORG]",
  "[CREDIT_CARD]",
  "[URL]",
  "[IBAN]",
  "[GROUP]",
  "[MEDICAL_ID]",
  "[DRIVER_LICENSE]",
];

// Dynamic placeholders: [URL:host] and numbered names ([NAME_1], ...). The URL
// interior is constrained to a real host (lowercase, a dotted TLD) or the
// literal "suspicious" — NOT `[^\]]+`. Otherwise an attacker smuggles PII past
// the gate by wrapping it: `[URL:contact alice@example.com]` would be blanked
// and the email would never be scanned. A real scrub.py placeholder is always
// `[URL:<lowercased host>]`/`[URL:suspicious]`, so PII (spaces, `@`, SSN
// digit-dashes with no TLD) can't masquerade as one.
const PII_ALLOWLIST_DYNAMIC = [
  // A real host with a dotted alphabetic TLD, an IPv4 host (the scrubber emits
  // [URL:192.168.1.1] for an IP URL — without this the inner IP would trip the
  // IPv4 detector and wrongly reject a correctly-scrubbed upload), or the
  // literal "suspicious". NOT [^\]]+, which would let PII be smuggled inside.
  /\[URL:(?:suspicious|[a-z0-9.-]+\.[a-z]{2,}|\d{1,3}(?:\.\d{1,3}){3})\]/g,
  /\[NAME_\d+\]/g,
];

export function checkPii(text) {
  // Unicode normalization first (defeats homoglyph/zero-width evasion)
  let cleaned = normalizeUnicode(text);
  // Replace allowlisted placeholders with a SPACE-PADDED sentinel. The padding
  // is essential: a bare sentinel glued to real PII ("bob@example.com[SSN]")
  // would consume the word boundary the \b-anchored PII patterns rely on, so
  // the email went undetected. The spaces restore the boundary so adjacent PII
  // is still caught, while the placeholder text itself can't self-match.
  for (const placeholder of PII_ALLOWLIST) {
    cleaned = cleaned.replaceAll(placeholder, " ___PLACEHOLDER___ ");
  }
  for (const dynamicPattern of PII_ALLOWLIST_DYNAMIC) {
    cleaned = cleaned.replace(dynamicPattern, " ___PLACEHOLDER___ ");
  }
  for (const { pattern, type } of PII_PATTERNS) {
    if (pattern.test(cleaned)) {
      return type;
    }
  }
  // Luhn-checked credit cards (after the structured patterns so a phone/SSN
  // keeps its own type, before the entropy backstop)
  if (hasCreditCard(cleaned)) {
    return "credit_card";
  }
  // IPv6 (full + compressed)
  if (hasIpv6(cleaned)) {
    return "ip";
  }
  // Entropy backstop for base64 / dotted secrets missed by explicit prefixes
  if (hasHighEntropySecret(cleaned)) {
    return "high_entropy_secret";
  }
  return null;
}

export function checkContent(text) {
  // Normalize, fold cross-script homoglyphs, then leet — match the blocklist
  // against each form so none is bypassable in isolation.
  const unicodeNormalized = normalizeUnicode(text);
  const folded = foldConfusables(stripMarks(unicodeNormalized));
  const leet = normalizeLeet(collapseSpaced(folded));
  for (const { pattern, category } of BLOCKLIST_PATTERNS) {
    if (
      pattern.test(unicodeNormalized) ||
      pattern.test(folded) ||
      pattern.test(leet)
    ) {
      return category;
    }
  }
  return null;
}
