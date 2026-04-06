# Design

## Why this exists

High-quality conversational data is the bottleneck for training open models. Common Parlance makes it easy for people using local AI to contribute their conversations to an open dataset — with privacy scrubbing, informed consent, and human review built in.

## Architecture

```
[AI Client] → [Proxy :11435] → [Local Model :11434]
                    ↓
              SQLite (raw logs)
                    ↓
              PII scrubbing (regex + optional NER)
                    ↓
              User review (approve/reject/edit)
                    ↓
              Cloudflare Worker (auth, validation, content filter)
                    ↓
              NER service (Presidio + spaCy, HuggingFace Spaces)
                    ↓
              HuggingFace dataset (JSONL, ODC-BY 1.0)
```

There are two ways to get conversations in: import existing exports, or capture live conversations through the proxy. Both feed into the same pipeline.

## Key design decisions

**Engine-agnostic proxy.** Works with any OpenAI-compatible or Ollama endpoint. The proxy adds near-zero latency — the bottleneck is always the model, never the proxy.

**Local-first privacy.** All PII scrubbing happens on the user's machine before anything is uploaded. The server runs a second NER pass as defense-in-depth, but the client never sends unscrubbed data.

**Auth proxy pattern.** Clients never talk to HuggingFace directly. A Cloudflare Worker validates API keys, checks content, and forwards to HuggingFace with server-held credentials. No secrets in the client package.

**Anonymous registration.** Device auth flow with Cloudflare Turnstile — no email, no account, no PII. API keys are opaque tokens with no identifying information.

**SQLite for local storage.** WAL mode, stdlib sqlite3, single file. Users can query their own data directly — inspectability is a trust feature.

**Typed PII placeholders.** `[NAME_1]`, `[EMAIL]`, `[PHONE]` instead of generic `[REDACTED]`. Preserves sentence structure and data utility for downstream consumers.

**JSONL uploads.** Simple, human-readable, debuggable. HuggingFace auto-converts to Parquet for consumers.

**ODC-BY 1.0 + Community Covenant.** Open Data Commons Attribution for legal clarity. A separate covenant requests (but cannot legally require) that model weights stay open.

## What gets published

Only human and assistant conversation turns. Everything else is stripped:

- Model names and engine metadata
- System prompts
- Token counts, timing, performance data
- All client metadata

## Content moderation

Layered approach: keyword content filter during processing, human review before upload, server-side validation, and community reporting for the published dataset.

## Tech stack

| Component | Technology |
|-----------|-----------|
| Client | Python 3.11+, FastAPI, httpx, SQLite |
| PII scrubbing | Regex + optional Presidio/spaCy |
| Upload proxy | Cloudflare Worker (JS) |
| NER service | FastAPI + Presidio on HuggingFace Spaces |
| Dataset | HuggingFace, JSONL format |
| Build | uv, hatchling, ruff, pytest |
