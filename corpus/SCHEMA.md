# cp-scrub golden corpus — schema

One language-neutral corpus of detection **ground truth**, consumed by per-language
runtimes (Python `scrub.py`, the JS Worker `content-filter.js`, the Presidio NER
service). It is both the **parity safety-net** (the three reimplementations can't
silently drift) and the **benchmark spine** (other scanners are scored against it).

Design: **one shared spec, per-runtime adapters.** A case stores what is sensitive
and what must survive — never any one implementation's output. Each runtime derives
its own assertion via a small mapping table (below). This mirrors how JSON Schema
Test Suite / CommonMark ship neutral cases and leave per-language conversion to the
implementer, and how Presidio's `presidio-research` models ground truth as spans.

## Layout

```
corpus/
  manifest.json                 spec_version, offset_unit, ner_models, dimensions[]
  SCHEMA.md                     this file
  cases/<dimension>.jsonl       one JSON case per line
```

Each manifest dimension carries a **`tier`** (`deterministic` | `ner`, default
`deterministic`) — see Tiers below.

## Case format (one JSON object per line)

```jsonc
{
  "id": "secret-openai-classic",        // unique, stable, kebab-case
  "surfaces": ["scrub", "worker"],       // which reimplementations this case pins
                                         // (also "ner" — the server NER service)
  "input": "key: sk-abc…789",            // raw text fed to the detector
  "sensitive": [                          // spans that MUST be redacted (may be [])
    {
      "entity_type": "secret/api_key",   // canonical type (hierarchical) — see map
      "entity_value": "sk-abc…789",      // the exact substring (Presidio Span field)
      "start_position": 5,                // codepoint offset, inclusive
      "end_position": 44,                 // codepoint offset, exclusive
      "verifiable": false,                // optional: live-verifiable (trufflehog-style) vs shape/entropy only
      "known_gap": {                      // optional: current behavior diverges from truth
        "disposition": "todo",            // "todo" (will fix) | "wontfix" (by design)
        "surfaces": ["scrub", "worker"],  // which surfaces currently miss it
        "reason": "entropy gate requires a digit; all-letter tokens leak"
      }
    }
  ],
  "guard": ["EndpointSuffix=core.windows.net"]   // substrings that MUST survive (over-redaction guard)
}
```

Field notes:
- **`entity_value` + offsets are both stored** and must agree: `input[start:end] ==
  entity_value` (codepoints). The redundancy is the point — the loader errors if they
  disagree, catching authoring mistakes. Offsets (not just substrings) are the field
  standard and disambiguate repeated substrings (e.g. coreferent names).
- **Offsets are Unicode codepoints**, not bytes. Python `str` is codepoints; the JS
  loader indexes via `Array.from(input)`; Presidio uses character positions. One unit,
  no UTF-8/UTF-16 conversion in the spec.
- **`guard`** entries are plain substrings (no offsets needed — presence/absence only).
- A case is a **positive** if it has any `sensitive` span, a pure **over-redaction
  guard** if `sensitive` is empty and `guard` is non-empty. It may be both.

## known_gap — pinning current behavior without lying

`known_gap` records that a surface's **current** behavior diverges from ground truth.
Convention borrowed from WPT's per-test `expected` and Chromium's split of transient
vs permanent expectations:
- The **parity test** pins current behavior — for a `known_gap` span on surface S, S
  is asserted to **leak** it (so documented behavior is green, and *closing* the gap
  goes red — the good kind, then you flip the flag).
- The **benchmark** scores against ground truth — the span still counts as a recall
  **miss**. One flag reconciles the regression-pin and the honest scoreboard.
- `disposition`: `"todo"` = a gap we intend to close; `"wontfix"` = a deliberate,
  permanent design choice (Chromium `NeverFixTests`). `surfaces` scopes the gap, since
  the three reimplementations can miss on different cases.

## Per-surface adapter tables

The canonical `entity_type` is engine-neutral. Each runtime maps it to its own vocab,
so a future engine that unifies the implementations changes the table, not the cases.

**Canonical → `scrub.py` placeholder** (the transformer asserts: `entity_value`
absent, placeholder present):

| canonical            | placeholder        |
|----------------------|--------------------|
| `secret/*`           | `[SECRET]`         |
| `email`              | `[EMAIL]`          |
| `phone`              | `[PHONE]`          |
| `ssn`                | `[SSN]`            |
| `ip`                 | `[IP]`             |
| `credit_card`        | `[CREDIT_CARD]`    |
| `path`               | `[PATH]`           |
| `url`                | `[URL:` (prefix)   |
| `name`               | `[NAME_` (prefix)  |
| `location`           | `[LOCATION]`       |

**Canonical → Worker `checkPii` type** (the gate asserts the returned type; `null`
means clean). The Worker has no NER, so `name`/`location` never list `worker`.

| canonical                  | worker type           |
|----------------------------|-----------------------|
| `secret/api_key`           | `api_key`             |
| `secret/jwt`               | `jwt`                 |
| `secret/private_key`       | `private_key`         |
| `secret/connection_string` | `connection_string`   |
| `secret/bearer_token`      | `bearer_token`        |
| `secret/gcp_key_id`        | `gcp_key_id`          |
| `secret/entropy`           | `high_entropy_secret` |
| `email`                    | `email`               |
| `ssn`                      | `ssn`                 |
| `phone`                    | `phone`               |
| `ip`                       | `ip`                  |
| `path`                     | `filepath`            |
| `credit_card`              | `credit_card`         |

The PII tier (`email`/`phone`/`name`/`location`/…) aligns with Presidio's entity
vocabulary so the corpus can feed Presidio's evaluator via its `entity_mapping`
(token/BIO scoring; one tokenization step). The `secret/*` subtree has no Presidio
entity and is scored on its own path.

**Canonical → NER surfaces (`scrub` PresidioScrubber + `ner` server service).**
Both NER reimplementations now produce the *same* placeholders — `PERSON` gets a
numbered `[NAME_n]` (reading order, coreference-preserving) and `LOCATION` a flat
`[LOCATION]`; both carry the same programming allow-list and per-entity thresholds.
They differ only by spaCy model (`scrub` = `en_core_web_lg`, `ner` = the deployed
`en_core_web_sm`), so the same case can pin both and a model-driven miss becomes a
per-surface `known_gap`.

| canonical  | scrub (lg) & ner (sm) placeholder |
|------------|-----------------------------------|
| `name`     | `[NAME_` (prefix, numbered)        |
| `location` | `[LOCATION]`                       |

**Coreference / numbering is derived, not stored.** The `[NAME_n]` number a name
gets is a function of the case's own spans: distinct casefolded `entity_value`
forms, in `start_position` order, map to `[NAME_1]`, `[NAME_2]`, … The NER suite
asserts that mapping (same surface form → same number; distinct forms → distinct
numbers; the count of distinct `[NAME_n]` in the output equals the count of
distinct non-gap name forms). No per-span `coref_group` field is needed — the
numbering falls out of the spans. NB: coreference is **string-identity** based, so
`Sarah Johnson` and a later bare `Sarah` are *two* numbers, not one (pinned).

## Tiers

| tier            | surfaces        | runs              | CI |
|-----------------|-----------------|-------------------|----|
| `deterministic` | `scrub`,`worker`| regex + workerd   | strict, blocking |
| `ner`           | `scrub`,`ner`   | Presidio (Python) | non-blocking, skipped if Presidio absent |

The deterministic tier is reproducible forever (regex). The `ner` tier is scored
against the spaCy models pinned in `manifest.ner_models`; its `known_gap` cases are
**model-version sensitive** — a model upgrade can flip a gap (good red → review the
corpus). That version-coupling is why the tier is non-blocking and Python-only
(workerd has no NER), and why the model versions are pinned in the manifest.

## Hygiene (public benchmark)

- **Synthetic / revoked secrets only** — never a live credential. The corpus stores
  secret *values* as substrings (unlike detect-secrets, which hashes); a leaked-but-
  live key here would be a real disclosure.
- Independently authored — **not** generated by any scanner being ranked (SecretBench
  contamination pitfall; see Benchmark Plan).

## Versioning

`manifest.json.spec_version` ties a parity baseline and a leaderboard score to a
corpus version (CommonMark publishes `spec.json` per spec version). Bump it on any
breaking schema or semantics change. `manifest.json.ner_models` pins the spaCy
model + version each NER surface is scored against, so an `ner`-tier score is
reproducible (and a model bump is a deliberate, visible manifest edit).
