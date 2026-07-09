---
language:
  - en
license: odc-by
pretty_name: Common Parlance Conversations
size_categories:
  - n<1K
task_categories:
  - text-generation
  - conversational
tags:
  - conversations
  - local-llm
  - privacy-preserving
  - pii-scrubbed
annotations_creators:
  - no-annotation
language_creators:
  - crowdsourced
source_datasets:
  - original
---

# Common Parlance Conversations

A community-contributed dataset of PII-scrubbed, metadata-stripped
conversations with local AI models. All conversations are voluntarily donated
by users who opted in,
reviewed their data, and approved it for publication.

## Dataset Details

- **Curated by:** Common Parlance contributors
- **License:** [ODC-BY 1.0](https://opendatacommons.org/licenses/by/1.0/) (Open Data Commons Attribution)
- **Community Covenant:** [COVENANT.md](https://github.com/common-parlance/common-parlance/blob/main/COVENANT.md) — a non-binding request to release model weights openly
- **Repository:** [common-parlance/common-parlance](https://github.com/common-parlance/common-parlance)

## Dataset Schema

Each record contains:

| Field | Type | Description |
|-------|------|-------------|
| `conversation_id` | string (UUID) | Randomly generated per upload, not traceable to contributor |
| `turns` | array of objects | Each has `role` ("user" or "assistant") and `content` (PII-scrubbed text) |
| `turn_count` | integer | Number of turns in the conversation |
| `language` | string | ISO 639-1 language code (detected via fasttext `lid.176.ftz` if available, otherwise `py3langid`, a pure-Python port of langid.py). Detection backend may vary by contributor installation, and may be unreliable for short, code-heavy, or mixed-language conversations. The dataset is tagged `en` because it is English-primary and PII scrubbing is English-only; non-English conversations may still appear in this field but receive weaker PII protection (see Bias, Risks, and Limitations). |
| `quality_signals` | object | See quality signals below |
| `ner_scrubbed` | boolean | Whether the contributor ran Presidio NER locally in addition to regex scrubbing. All records are sent through server-side NER regardless of this flag; however, if the NER service is temporarily unavailable, the upload is rejected and retried later. |

### Quality Signals

| Field | Type | Description |
|-------|------|-------------|
| `avg_response_len` | integer | Average assistant response length in characters (computed on scrubbed text†) |
| `has_code` | boolean | Whether the conversation contains code blocks |
| `vocab_diversity` | float (0–1) | Type-token ratio across all turns. Decreases with text length — not comparable across conversations of different lengths.† |
| `total_length` | integer | Total character count across all turns (computed on scrubbed text†) |
| `user_msg_count` | integer | Number of user messages |
| `assistant_msg_count` | integer | Number of assistant messages |

† Length-based signals and vocabulary metrics are computed on PII-scrubbed text, where
placeholders like `[NAME]` and `[EMAIL]` replace original content. Values may be slightly
skewed for conversations with heavy PII replacement.

### What is NOT in the data

- Model names or engine metadata
- System prompts
- Token counts, timing, or performance data
- IP addresses, user agents, or client metadata
- Timestamps (neither conversation time nor upload time)
- Any user or device identifier

## Example Record

```json
{
  "conversation_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "turns": [
    {"role": "user", "content": "Can you help me debug this? [NAME_1] from my team said the API returns 500 when I hit [URL:example.com]"},
    {"role": "assistant", "content": "Sure! A 500 on that endpoint usually means a database connection issue. Can you check if your connection string in the config is correct? Make sure the host and port match your database server."},
    {"role": "user", "content": "Found it — the password had a special character that wasn't escaped. Thanks!"},
    {"role": "assistant", "content": "That's a common gotcha. You can URL-encode special characters in the connection string, or use a config file that handles them natively."}
  ],
  "turn_count": 4,
  "language": "en",
  "quality_signals": {
    "avg_response_len": 187,
    "has_code": false,
    "vocab_diversity": 0.72,
    "total_length": 502,
    "user_msg_count": 2,
    "assistant_msg_count": 2
  },
  "ner_scrubbed": true
}
```

## Dataset Statistics

Statistics will be published after initial data collection.

<!-- TODO: add total conversations, language distribution, avg turns,
     quality signal distributions, etc. -->

## Collection Process

1. Contributors install the Common Parlance proxy, which sits between their AI
   client and their local model engine (Ollama, llama.cpp, vLLM, etc.)
2. Conversations are captured to a local SQLite database
3. PII is scrubbed locally via regex (emails, phones, SSNs, IPs, file paths,
   API keys, credit cards, secrets) and optionally via local NER (Presidio + spaCy)
4. Content is checked against a blocklist for harmful material (this check runs
   on the original text before scrubbing, so it sees full context for filtering).
   When the optional `[ml]` extra (Detoxify) is installed, an ML toxicity filter
   runs as a second layer to catch contextual toxicity that keyword matching
   misses.
5. Contributors review and approve each conversation (or enable auto-approve)
6. Approved conversations are uploaded through an auth proxy that performs:
   - Server-side PII regex validation (rejects if structured PII detected)
   - Server-side NER pass (Presidio + spaCy) for names and locations
     (organization/product names are intentionally not redacted — noisy NER,
     high utility and low risk in technical text)
   - Content filter check
7. Data that passes all checks is committed to this dataset

All contributors explicitly opt in via an interactive consent prompt. The proxy
functions normally without consent — it simply does not log or upload.

## PII Scrubbing Methodology

Two-stage pipeline:

**Stage 1 — Local (on contributor's machine):**
- Regex patterns for: emails, phone numbers, SSNs, credit card numbers, IP
  addresses, file paths, API keys/secrets, URLs
- Optional: Presidio + spaCy NER for names, addresses, locations
- All detected PII replaced with typed placeholders (e.g., `[NAME_1]`, `[EMAIL]`,
  `[PHONE]`) to preserve conversational structure

**Stage 2 — Server-side (before publication):**
- Regex validation rejects uploads containing detectable structured PII
- NER pass (Presidio + spaCy `en_core_web_sm`) scrubs names and locations
  that regex cannot detect (organization/product names are intentionally not
  redacted)
- Scrubbed entities replaced with the same typed placeholder format

### Known limitations

- NER models are English-only (`en_core_web_sm`). Names and locations in other
  languages may not be detected by the server-side pass.
- Regex cannot catch all forms of unstructured PII (e.g., "my neighbor who
  teaches at the school on Oak Street").
- Contributors are encouraged to review conversations before approving.
- The `quality_signals` schema may evolve across dataset versions as new
  signals are added. Consumers should handle missing fields gracefully.

## Content Moderation & Reporting

Contributions pass through a layered content filter before publication, following
the approach used for comparable open conversation datasets (e.g. WildChat,
LMSYS-Chat-1M): automated filtering + contributor review + a reporting-and-removal
path.

- **Keyword blocklist** targeting CSAM indicators and dangerous instructions,
  applied on the client and **mirrored server-side** so it cannot be bypassed by
  modifying the client. Matching runs after a normalization pass (Unicode NFKC,
  invisible/bidirectional-control stripping, combining-mark removal, cross-script
  homoglyph folding, single-letter-spacing collapse, and leetspeak) so common
  evasions do not slip past trivially.
- **Optional ML toxicity filter** (Detoxify) as a second client-side layer when
  the `[ml]` extra is installed.
- **Contributor review** — each conversation can be reviewed and rejected before
  upload.

Blocked content is discarded, never staged or published.

**Known limitations (stated plainly).** Keyword and general-toxicity filters cannot
catch *meaning*. Content that is semantically harmful but lexically clean —
variable-chunk spacing (e.g. `ch i ld`), algospeak/euphemism, or grooming and
enticement phrased in ordinary language — is **not reliably caught** by the current
filters; a dedicated CSAM/grooming classifier is a planned addition, not yet
deployed. As with any crowdsourced text dataset, some objectionable content or
residual PII may reach the published data despite these measures.

**Reporting.** If you find harmful, illegal, or personally identifiable content in
the dataset, please report it using the
[Report Harmful Content](https://github.com/common-parlance/common-parlance/issues/new?template=content_report.yml)
issue template (describe the content and where it is — do **not** paste the content
itself). We take reports seriously and respond promptly: reported content is removed
from the dataset, and reports involving CSAM or illegal content are prioritized.

## Intended Uses

- Research on human-AI conversation patterns
- Evaluating conversational AI quality and diversity
- Studying how people interact with locally-hosted language models

## Out-of-Scope Uses

- Attempting to re-identify contributors from writing style or conversation content
- Building user profiles or behavioral models of individual contributors
- Any use that violates the [ODC-BY 1.0 license](https://opendatacommons.org/licenses/by/1.0/)

## Bias, Risks, and Limitations

- **Contributor demographics:** Contributors are people who run local AI models,
  which skews toward technically proficient, English-speaking users. The dataset
  is not representative of the general population.
- **Local model bias:** Conversations are with locally-hosted models, which may
  differ in capability and behavior from commercial API models. Response quality
  depends on the contributor's hardware and model choice.
- **Language coverage:** NER scrubbing is English-only. Non-English conversations
  may have weaker PII protection.
- **Residual PII risk:** Despite two-stage scrubbing, some PII may survive —
  particularly unstructured references in non-English text or unusual formats.
- **Content filtering:** Automated filtering is blocklist- and toxicity-based and
  cannot catch semantically harmful but lexically clean content (see
  [Content Moderation & Reporting](#content-moderation--reporting)). The dataset
  may contain conversations that some users find objectionable; please report
  harmful content via the issue template linked there.

## Citation

To be determined.

<!-- TODO: add BibTeX citation when available -->

## Attribution

> Common Parlance Contributors — [common-parlance/conversations](https://huggingface.co/datasets/common-parlance/conversations) (ODC-BY 1.0)
