/**
 * Content filtering and PII validation.
 *
 * Server-side defense-in-depth checks that run before accepting uploads.
 * Mirrors client-side patterns in scrub.py and filter.py.
 */

// --- Content filter (server-side, mirrors client blocklist) ---
// Keep in sync with blocklists/*.txt in the Python package.

export const BLOCKLIST_PATTERNS = [
  // CSAM indicators
  {
    pattern: /child\s+(?:porn|sex|abuse|exploit|erotic|nude)/i,
    category: "csam_indicator",
  },
  {
    pattern: /minor\s+(?:porn|sex|abuse|exploit|erotic|nude)/i,
    category: "csam_indicator",
  },
  {
    pattern: /underage\s+(?:porn|sex|abuse|exploit|erotic|nude)/i,
    category: "csam_indicator",
  },
  {
    pattern:
      /(?:porn|sex|erotic|nude)\s+(?:child|minor|underage|preteen|prepubescent)/i,
    category: "csam_indicator",
  },
  { pattern: /pedophil[ei]/i, category: "csam_indicator" },
  {
    pattern: /preteen\s+(?:sex|nude|erotic|naked)/i,
    category: "csam_indicator",
  },
  {
    pattern:
      /(?:sex|erotic|naked|nude)\s+(?:boy|girl)\s+(?:young|little|small)/i,
    category: "csam_indicator",
  },

  // Dangerous instructions
  {
    pattern:
      /how\s+to\s+(?:make|build|create|synthesize)\s+(?:a\s+)?(?:bomb|explosive|nerve\s+agent|sarin|anthrax|ricin|mustard\s+gas)/i,
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
  // Apply character substitutions
  return collapsed.replace(/[@48(3!|0$57+]/g, (ch) => LEET_MAP[ch] || ch);
}

// --- Unicode normalization (adversarial PII evasion defense) ---
// Homoglyphs (Cyrillic а vs Latin a), zero-width characters, and bidi
// overrides can bypass both regex and NER. NFKC normalization + control
// character stripping defeats these attacks. Must run before any pattern
// matching or content checks.

// Invisible/control characters that break tokenization without changing
// visible text: zero-width spaces/joiners, bidi overrides, etc.
const INVISIBLE_RE =
  /[\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2061\u2062\u2063\u2064\ufeff\ufff9\ufffa\ufffb]+/g;

function normalizeUnicode(text) {
  // NFKC normalization maps homoglyphs to canonical Latin forms
  // (Cyrillic а → a, fullwidth Ａ → A, mathematical 𝐀 → A)
  let normalized = text.normalize("NFKC");
  // Strip invisible characters
  normalized = normalized.replace(INVISIBLE_RE, "");
  return normalized;
}

// --- Server-side PII validation (defense-in-depth) ---
// Keep in sync with scrub.py _SECRET_PREFIX_PATTERNS, _SECRET_BLOCK_PATTERNS,
// _FILE_PATH_PATTERNS, and _PII_PATTERNS. This is the trust boundary — if the
// client scrubber misses something (or is bypassed), these catch it.

const PII_PATTERNS = [
  // Structured PII (scrub.py _PII_PATTERNS)
  {
    pattern: /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/,
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
  { pattern: /\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b/, type: "ip" },
  {
    pattern: /\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b/,
    type: "ip",
  },

  // File paths that leak usernames (scrub.py _FILE_PATH_PATTERNS)
  { pattern: /\/Users\/[a-zA-Z0-9_.-]+/, type: "filepath" },
  { pattern: /\/home\/[a-zA-Z0-9_.-]+/, type: "filepath" },
  { pattern: /C:\\Users\\[a-zA-Z0-9_.-]+/i, type: "filepath" },
  { pattern: /\/root\b/, type: "filepath" },

  // Known API key prefixes (scrub.py _SECRET_PREFIX_PATTERNS)
  { pattern: /\b(?:sk-[a-zA-Z0-9]{20,})\b/, type: "api_key" },
  { pattern: /\b(?:sk-ant-[a-zA-Z0-9\-]{20,})\b/, type: "api_key" },
  { pattern: /\b(?:ghp_[a-zA-Z0-9]{36,})\b/, type: "api_key" },
  { pattern: /\b(?:gho_[a-zA-Z0-9]{36,})\b/, type: "api_key" },
  { pattern: /\b(?:glpat-[a-zA-Z0-9\-_]{20,})\b/, type: "api_key" },
  { pattern: /\b(?:xoxb-[a-zA-Z0-9\-]{20,})\b/, type: "api_key" },
  { pattern: /\b(?:xoxp-[a-zA-Z0-9\-]{20,})\b/, type: "api_key" },
  { pattern: /\b(?:AKIA[0-9A-Z]{16})\b/, type: "api_key" },
  { pattern: /\b(?:hf_[a-zA-Z0-9]{20,})\b/, type: "api_key" },
  { pattern: /\b(?:npm_[a-zA-Z0-9]{36,})\b/, type: "api_key" },
  { pattern: /\b(?:pypi-[a-zA-Z0-9]{20,})\b/, type: "api_key" },
  { pattern: /\b(?:AIza[a-zA-Z0-9\-_]{35})\b/, type: "api_key" },
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
  { pattern: /-----BEGIN [A-Z ]+PRIVATE KEY-----/, type: "private_key" },
  { pattern: /-----BEGIN PGP PRIVATE KEY BLOCK-----/, type: "private_key" },
  {
    pattern: /\b(?:postgres|mysql|mongodb|redis):\/\/[^\s]+:[^\s]+@[^\s]+/,
    type: "connection_string",
  },
  {
    pattern: /Authorization:\s*Bearer\s+[a-zA-Z0-9\-_.]+/i,
    type: "bearer_token",
  },
];

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

// Dynamic placeholders that use [TYPE:value] format (e.g. [URL:example.com])
const PII_ALLOWLIST_DYNAMIC = /\[URL:[^\]]+\]/g;

export function checkPii(text) {
  // Unicode normalization first (defeats homoglyph/zero-width evasion)
  let cleaned = normalizeUnicode(text);
  for (const placeholder of PII_ALLOWLIST) {
    cleaned = cleaned.replaceAll(placeholder, "___PLACEHOLDER___");
  }
  cleaned = cleaned.replace(PII_ALLOWLIST_DYNAMIC, "___PLACEHOLDER___");
  for (const { pattern, type } of PII_PATTERNS) {
    if (pattern.test(cleaned)) {
      return type;
    }
  }
  return null;
}

export function checkContent(text) {
  // Unicode normalization first (defeats homoglyph/zero-width evasion)
  const unicodeNormalized = normalizeUnicode(text);
  const normalized = normalizeLeet(unicodeNormalized);
  for (const { pattern, category } of BLOCKLIST_PATTERNS) {
    if (pattern.test(unicodeNormalized) || pattern.test(normalized)) {
      return category;
    }
  }
  return null;
}
