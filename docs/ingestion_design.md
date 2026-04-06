# Ingestion Architecture Design

## Problem

The current proxy-only capture severely limits what conversations can be contributed:
- Only works with local models over HTTP (Ollama, llama.cpp, etc.)
- Requires users to change their client's endpoint URL
- Can't reach historical conversations
- Doesn't capture agent/tool-use metadata
- No way to contribute existing conversation exports

## Two Datasets

| Dataset | Repo | Content | Routing signal |
|---------|------|---------|----------------|
| Conversations | `common-parlance/conversations` | Human ↔ assistant chat | Only `user`/`assistant` roles, no tool calls |
| Agent Metadata | `common-parlance/agent-metadata` | Structural traces (Tier 0) | Has tool calls — content stripped, metadata only |

The app detects which dataset a conversation belongs to and routes automatically.
Conversations get the full scrub → review → upload pipeline.
Agent metadata is content-free by design — no scrubbing needed.

## Launch Plan

### Phase 1: Conversations (launch)
- `import` command for file ingestion
- `proxy` for live local model capture
- Existing scrub → review → upload pipeline

### Phase 2: Agent Metadata — Tier 0
- LiteLLM callback AND OTEL exporter shipped simultaneously
- Structural metadata only: tool names, call sequences, success/fail, latency, tokens
- No conversation content captured — zero privacy risk
- Novel dataset: nobody has this at scale from real-world usage
- OTEL note: GenAI conventions are "Development" status (unstable, broke in v1.37).
  Pin to specific semconv version, use `OTEL_SEMCONV_STABILITY_OPT_IN` mechanism.
  Tool names require parsing opt-in `gen_ai.input.messages` attribute.


## Ingestion Paths

### Path 1: `common-parlance import <file|directory>`

Import existing conversation exports. Lowest friction — users already have data.

`--watch` flag re-runs on an interval, tracking a cursor to avoid re-importing.

**Supported formats (prioritized):**

| Format | Source | Structure | Complexity |
|--------|--------|-----------|------------|
| OpenAI messages JSONL | API logs, many tools | `{"messages": [{"role", "content"}]}` | Low |
| ShareGPT JSONL/JSON | HuggingFace datasets, tools | `{"conversations": [{"from", "value"}]}` | Low |
| ChatGPT export ZIP | ChatGPT Settings > Export | Tree in `conversations.json`, flatten via parent/children | Medium |
| Claude export ZIP | Claude Settings > Export | JSON in ZIP | Medium |
| Open WebUI SQLite | `~/.open-webui/data/webui.db` | JSON blob in `chat` column | Medium |
| Jan.ai threads | `~/jan/threads/thread_*/messages.jsonl` | One message per line | Low |
| SillyTavern JSONL | `data/default-user/chats/` | Header line + messages | Low |
| oobabooga JSON | `user_data/logs/chat/` | `internal`/`visible` arrays | Low |

**Auto-discovery for `--watch` mode:**

| App | Path (macOS) | Path (Linux) | Format |
|-----|-------------|--------------|--------|
| Open WebUI | `~/.open-webui/data/webui.db` | same | SQLite |
| Jan.ai | `~/jan/threads/` | `~/jan/threads/` | JSONL |
| SillyTavern | (install-relative) | same | JSONL |
| oobabooga | (install-relative) | same | JSON |

**Design:**
- Auto-detect format from file extension + content sniffing
- `--format` flag to override detection
- Each format gets an extractor → normalizes to internal schema
- Dedup against existing DB by content hash
- `--watch` polls on interval, tracks last-seen cursor per source

### Path 2: `common-parlance proxy` (existing, keep as-is)

Live HTTP capture for local model chat:

```
[AI Client :11435] → [proxy] → [Ollama :11434]
```

Handles Ollama (`/api/chat`, `/api/generate`) and OpenAI (`/v1/chat/completions`).

### Path 3: Agent Metadata Collection (integrations)

Instead of building a competing proxy, integrate with existing infrastructure
via callbacks/exporters. Three integration points, shipped in priority order:

#### 3a. LiteLLM Callback (ship with OTEL)

```python
# pip install common-parlance[litellm]
import litellm
from common_parlance.integrations.litellm import CommonParlanceLogger

litellm.callbacks = [CommonParlanceLogger()]
```

Or in LiteLLM proxy YAML:
```yaml
litellm_settings:
  callbacks: ["common_parlance"]
```

- Inherits `CustomLogger`, ~50 lines
- Extracts: model, tool names, success/fail, token counts, latency, cost
- Discards: messages, response content, system prompts
- Covers: LiteLLM proxy users, CrewAI (bundles LiteLLM)
- Effort: Days
- Risk: LiteLLM has reliability issues (1900+ open issues, memory leaks,
  thin company backing at $2.1M raised). Keep integration as single thin file.

#### 3b. OpenTelemetry Exporter (ship with LiteLLM)

```python
# pip install common-parlance[otel]
from common_parlance.integrations.otel import CommonParlanceExporter
from opentelemetry.sdk.trace.export import BatchSpanProcessor

provider.add_span_processor(BatchSpanProcessor(CommonParlanceExporter()))
```

- Implements OTEL `SpanExporter` interface
- Reads GenAI semantic convention attributes (model, tokens, tool names, latency)
- Vendor-neutral CNCF standard backed by Amazon, Google, Microsoft, Elastic
- Covers: anything OTEL-instrumented (OpenAI SDK, Anthropic SDK, LangChain
  via OpenLLMetry, LiteLLM, Bedrock, Azure OpenAI)
- Caveat: GenAI conventions still "Development" status, may change
- Effort: Weeks
- This is the future-proof path

#### 3c. Direct SDK Wrappers (optional)

```python
# pip install common-parlance[openai]
from common_parlance.integrations.openai import instrument
instrument()  # patches the default client
```

- Wraps OpenAI + Anthropic Python SDKs via httpx transport hooks
- Covers the 236M monthly direct SDK downloads
- Streaming is fragile (needs custom `BaseTransport` subclass)
- Only 2 providers
- Effort: Medium-high

**Coverage estimate:**
- 3a alone: ~30-40% of Python AI ecosystem
- 3a + 3b: ~70-80%
- All three: ~90%+

**Skip:** LangChain callback — their users have Langfuse/LangSmith, OTEL covers them.

## Storage

Conversations and agent metadata use **separate SQLite databases**:

| DB | Default Path | Purpose |
|----|-------------|---------|
| `conversations.db` | `~/.local/share/common-parlance/conversations.db` | Conversation pipeline (capture → scrub → review → upload) |
| `agent_metadata.db` | `~/.local/share/common-parlance/agent_metadata.db` | Tier 0 traces (append → batch upload, no scrub/review) |

Rationale: conversations have a multi-stage pipeline (raw exchanges, scrubbing state,
review status, dead letters). Agent metadata is append-only with batch upload — no
scrubbing needed, no review step. Mixing them would complicate both schemas and
make the conversation DB's `secure_delete` / purge logic harder to reason about.

## Tier 0 Agent Metadata Schema

Content-free structural traces. No PII, no scrubbing needed.

```jsonl
{
  "trace_id": "uuid",
  "timestamp": "ISO-8601",
  "model": "gpt-4o",
  "provider": "openai",
  "steps": [
    {"tool": "file_read", "success": true, "latency_ms": 50, "tokens_in": 200, "tokens_out": 800},
    {"tool": "execute_bash", "success": false, "latency_ms": 3000, "tokens_in": 600, "tokens_out": 50},
    {"tool": "execute_bash", "success": true, "latency_ms": 1200, "tokens_in": 450, "tokens_out": 120}
  ],
  "outcome": "success",
  "total_tokens": 2270,
  "total_cost_usd": 0.034,
  "total_steps": 3,
  "total_duration_ms": 4250,
  "has_tool_use": true,
  "tool_names": ["file_read", "execute_bash"],
  "source_integration": "litellm"
}
```

**Research value (demonstrated, not speculative):**
- Cost optimization: 60-70% of agent tokens go to file reading, not generation
- Failure prediction: repetitive tool calls (3+ same in a row) strongly predict failure
- Behavioral fingerprinting: different agents show distinct tool call distributions
- Model routing: structural features predict which model is cheapest for success
- Waste detection: "stuck" agents detectable from step count + tool repetition

**What doesn't exist yet:** Large-scale, real-world Tier 0 data from production agents.
OpenRouter has this data but keeps it proprietary. Benchmarks (SWE-bench, GAIA) only
have controlled experiments. A public dataset would be genuinely novel.

## Format Detection Heuristic (for `import`)

```
1. ZIP file?
   → Contains conversations.json with "mapping" keys? → ChatGPT export
   → Contains JSON with role/content arrays? → Claude export

2. SQLite file?
   → Has "chat" table with "chat" JSON column? → Open WebUI
   → Has "exchanges" table? → Common Parlance DB (skip)

3. JSONL file?
   → Lines have "messages" with role/content? → OpenAI format
   → Lines have "conversations" with from/value? → ShareGPT format
   → Lines have "mes" and "is_user"? → SillyTavern
   → Lines have "thread_id" and nested content? → Jan.ai

4. JSON file?
   → Has "internal"/"visible" arrays? → oobabooga
   → Has "conversations" with from/value? → ShareGPT (single)
   → Array with "messages"? → OpenAI format (batch)

5. Directory?
   → Contains messages.jsonl + thread.json? → Jan.ai thread
   → Contains *.jsonl? → SillyTavern chat dir
   → Recurse and process individual files
```

## Open Questions

1. **Conversation source attribution**: Record broad type (`chat`, `import`) not
   app-specific to avoid fingerprinting?

3. **Agent metadata granularity**: Should we capture tool argument *types* (string,
   int, object) without values? Or strictly tool names only?

4. **Metadata batching**: Buffer locally and upload in daily batches (like
   conversations) or stream continuously?

5. **`proxy` + `import` consolidation**: The proxy could auto-detect format and
   handle both Ollama-style and OpenAI API-style requests. Worth merging?

## Existing Art

**Conversation collection:**
- WildChat (4.8M convos, free GPT proxy) — closest to our approach
- ShareGPT (browser extension, dead) — went viral
- LMSYS Arena (self-hosted UI, 1M+ convos)
- OpenAssistant (crowdsourcing, dead) — highest quality

**Agent metadata / observability:**
- OpenRouter State of AI — analyzed 100T tokens of metadata, kept data proprietary
- LiteLLM — gateway with logging, MIT core
- Helicone — Rust gateway, fast, self-hostable
- Langfuse — SDK-based tracing, MIT, best nested trace viz
- OpenLLMetry — OTEL-native LLM instrumentation

**Agent trace datasets (all include content, not metadata-only):**
- Toolathlon-Trajectories (5K records, closest to Tier 0 if you extract key_stats)
- PatronusAI/TRAIL (148 traces, OTEL spans, gated)
- NVIDIA Nemotron-SWE-v1 (59K trajectories, CC-BY-4.0)
- Agent Data Protocol (1.3M trajectories, ICLR 2026)

**Gap we fill:** No public, large-scale, real-world Tier 0 agent metadata dataset exists.

## Implementation Priority

### Phase 1: Conversations (launch)
1. `import` with OpenAI messages JSONL + ShareGPT
2. `import` with ChatGPT export ZIP
3. `import` with Open WebUI SQLite
4. `import --watch` mode
5. Remaining import formats

### Phase 2: Agent Metadata
6. LiteLLM callback + OTEL exporter (Tier 0 metadata only, ship together)
7. Agent metadata dataset repo + schema
8. Direct SDK wrappers (optional)

