/**
 * Upload handling: JSONL validation, content/PII checks, NER scrubbing,
 * HuggingFace forwarding, and contribution tracking.
 */

import { checkPii, checkContent } from "./content-filter.js";
import {
  jsonResponse,
  incrementMetric,
  incrementMetricBy,
  checkGlobalRateLimit,
  incrementGlobalRateLimit,
  checkRateLimit,
  incrementRateLimit,
  contentHashHex,
  checkContentHash,
  recordContentHash,
  validateApiKey,
  decayTier,
  apiKeyId,
} from "./helpers.js";
import { KEY_COOLDOWN_SECONDS } from "./registration.js";

// --- Contribution tracking ---
// Batch-level attribution with auto-expiry for rollback capability.
// Maps API key → batch filenames for 90 days, then auto-deletes.
const CONTRIBUTION_TTL = 90 * 24 * 3600; // 90 days

const MAX_UPLOAD_BYTES = 2 * 1024 * 1024;

// The exact fields the reference client emits. validateJsonl rejects any
// others: only turn.content is scanned by checkContent/checkPii, so an unknown
// top-level field (e.g. "notes") or turn field would otherwise be accepted AND
// preserved into the published record (the upload path spreads ...record),
// smuggling unscanned free text into the public dataset.
const ALLOWED_RECORD_KEYS = new Set([
  "conversation_id",
  "turns",
  "turn_count",
  "language",
  "quality_signals",
  "ner_scrubbed",
]);
const ALLOWED_TURN_KEYS = new Set(["role", "content"]);
// The published dataset is defined as human+assistant turns only; system/tool
// turns (and system prompts) are stripped client-side (extract.py filters to
// user/assistant). Enforce that at the trust boundary so a non-reference or
// bypassing client can't smuggle a system prompt into the public dataset.
const ALLOWED_ROLES = new Set(["user", "assistant"]);

// --- JSONL validation ---

export function validateJsonl(text) {
  const lines = text.trim().split("\n");
  const records = [];

  for (let i = 0; i < lines.length; i++) {
    let record;
    try {
      record = JSON.parse(lines[i]);
    } catch {
      return { error: `Invalid JSON on line ${i + 1}` };
    }

    if (!Array.isArray(record.turns)) {
      return { error: `Line ${i + 1}: missing turns array` };
    }
    // conversation_id is interpolated into error/log lines and published to the
    // dataset, so constrain it to a bounded safe-charset token (the reference
    // client emits a uuid4). Rejecting whitespace/control chars closes a
    // log-injection vector and keeps malformed ids out of the corpus.
    if (
      typeof record.conversation_id !== "string" ||
      !/^[A-Za-z0-9_-]{1,128}$/.test(record.conversation_id)
    ) {
      return {
        error: `Line ${i + 1}: conversation_id must match [A-Za-z0-9_-]{1,128}`,
      };
    }

    // Validate metadata field types to prevent schema drift in the dataset
    if (typeof record.turn_count !== "number" || !Number.isInteger(record.turn_count)) {
      return { error: `Line ${i + 1}: turn_count must be an integer` };
    }
    // language is a detected language code (e.g. "en", "zh-cn") or null — NOT
    // free text. Constraining it (and role/quality_signals below) closes a PII
    // channel: content/PII filters only scan turn.content, so a record with
    // `language: "my SSN is 123-45-6789"` or a string-valued quality signal
    // would otherwise carry PII to the dataset unscanned.
    if (
      record.language !== null &&
      (typeof record.language !== "string" ||
        !/^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$/.test(record.language))
    ) {
      return { error: `Line ${i + 1}: language must be a BCP47-style code or null` };
    }
    if (record.quality_signals === null || typeof record.quality_signals !== "object" || Array.isArray(record.quality_signals)) {
      return { error: `Line ${i + 1}: quality_signals must be an object` };
    }
    // quality_signals carries only numeric/boolean metrics — reject string or
    // nested values that could smuggle free text.
    for (const v of Object.values(record.quality_signals)) {
      if (typeof v !== "number" && typeof v !== "boolean") {
        return {
          error: `Line ${i + 1}: quality_signals values must be numbers or booleans`,
        };
      }
    }
    if (typeof record.ner_scrubbed !== "boolean") {
      return { error: `Line ${i + 1}: ner_scrubbed must be a boolean` };
    }
    // Strict schema: reject unknown top-level fields so they can't ride into
    // the published record unscanned (see ALLOWED_RECORD_KEYS above).
    for (const key of Object.keys(record)) {
      if (!ALLOWED_RECORD_KEYS.has(key)) {
        return { error: `Line ${i + 1}: unexpected field "${key}"` };
      }
    }

    for (const turn of record.turns) {
      // Each turn must be a plain object — a null/array/primitive element would
      // throw on turn.role below (→ 500). Reject as a clean 422 instead.
      if (turn === null || typeof turn !== "object" || Array.isArray(turn)) {
        return { error: `Line ${i + 1}: each turn must be an object` };
      }
      // Require strings, not just truthy — a non-string content (number,
      // object, array, true) would pass a truthiness check, then throw in
      // normalizeUnicode().normalize() (→ 500, and the content/PII filters
      // silently skipped). Reject as a clean 422 instead.
      if (typeof turn.role !== "string" || typeof turn.content !== "string") {
        return { error: `Line ${i + 1}: turn role and content must be strings` };
      }
      // Only user/assistant turns are publishable (see ALLOWED_ROLES) — this
      // also keeps role from being a free-text channel that carries PII.
      if (!ALLOWED_ROLES.has(turn.role)) {
        return { error: `Line ${i + 1}: turn role must be "user" or "assistant"` };
      }
      // Reject unknown turn fields for the same reason as the record-level
      // allowlist — only content is scanned.
      for (const key of Object.keys(turn)) {
        if (!ALLOWED_TURN_KEYS.has(key)) {
          return { error: `Line ${i + 1}: unexpected turn field "${key}"` };
        }
      }

      const blocked = checkContent(turn.content);
      if (blocked) {
        return {
          error: `Content blocked (${blocked}) in conversation ${record.conversation_id}`,
        };
      }

      const piiType = checkPii(turn.content);
      if (piiType) {
        return {
          error: `Unscrubbed PII detected (${piiType}) in conversation ${record.conversation_id}`,
        };
      }
    }

    records.push(record);
  }

  return { records, count: records.length };
}

// --- HuggingFace upload ---

async function uploadToHuggingFace(jsonlContent, env) {
  const repo = env.HF_REPO || "common-parlance/conversations";
  const date = new Date().toISOString().slice(0, 10);
  const suffix = Math.random().toString(36).slice(2, 8);
  const filePath = `data/batch_${date}_${suffix}.jsonl`;

  const header = JSON.stringify({
    key: "header",
    value: {
      summary: `Add conversations (${date})`,
      description: "Uploaded via Common Parlance proxy",
    },
  });

  // Base64 encode the JSONL content
  const contentBytes = new TextEncoder().encode(jsonlContent);
  let base64Content = "";
  const chunk = 8192;
  for (let i = 0; i < contentBytes.length; i += chunk) {
    base64Content += String.fromCharCode(...contentBytes.subarray(i, i + chunk));
  }
  base64Content = btoa(base64Content);

  const fileOp = JSON.stringify({
    key: "file",
    value: {
      content: base64Content,
      path: filePath,
      encoding: "base64",
    },
  });

  const body = header + "\n" + fileOp;

  const hfApiBase = (env.HF_API_BASE || "https://huggingface.co").replace(/\/$/, "");
  const response = await fetch(
    `${hfApiBase}/api/datasets/${repo}/commit/main`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.HF_TOKEN}`,
        "Content-Type": "application/x-ndjson",
      },
      body,
    }
  );

  if (!response.ok) {
    const status = response.status;
    console.error(`HuggingFace API error: ${status}`);
    throw new Error(`upstream storage error (HTTP ${status})`);
  }

  const result = await response.json();
  return { ...result, _filePath: filePath };
}

// --- Server-side NER scrubbing ---

const NER_BATCH_SIZE = 200; // NER service MAX_TURNS limit

async function callNer(turns, nerUrl, nerApiKey) {
  const response = await fetch(`${nerUrl.replace(/\/$/, "")}/scrub`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(nerApiKey ? { "X-API-Key": nerApiKey } : {}),
    },
    body: JSON.stringify({ turns }),
    signal: AbortSignal.timeout(25000),
  });

  if (!response.ok) {
    throw new Error(`NER service returned ${response.status}`);
  }

  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error("NER service returned invalid JSON");
  }

  if (!result.turns || !Array.isArray(result.turns)) {
    throw new Error("NER service returned unexpected response format");
  }

  if (result.turns.length !== turns.length) {
    throw new Error(
      `NER turn count mismatch: sent ${turns.length}, got ${result.turns.length}`
    );
  }

  return result;
}

async function scrubViaNer(records, nerUrl, nerApiKey) {
  const turnCounts = records.map((r) => r.turns.length);
  const allTurns = records.flatMap((r) => r.turns);

  // Split into batches of NER_BATCH_SIZE to stay within the NER
  // service's MAX_TURNS limit. Process sequentially to avoid
  // overwhelming the service.
  const scrubbedTurns = [];
  const perTurnCounts = [];

  for (let i = 0; i < allTurns.length; i += NER_BATCH_SIZE) {
    const batch = allTurns.slice(i, i + NER_BATCH_SIZE);
    const result = await callNer(batch, nerUrl, nerApiKey);
    scrubbedTurns.push(...result.turns);
    if (result.entities_per_turn) {
      perTurnCounts.push(...result.entities_per_turn);
    } else {
      // Fallback: attribute all entities to first turn in batch
      for (let j = 0; j < batch.length; j++) {
        perTurnCounts.push(j === 0 ? (result.entities_found || 0) : 0);
      }
    }
  }

  // Reassemble: split scrubbed turns back into per-record groups.
  const results = [];
  let offset = 0;
  for (let i = 0; i < records.length; i++) {
    const count = turnCounts[i];
    const { turns: _origTurns, ...metadata } = records[i];
    const recordEntities = perTurnCounts
      .slice(offset, offset + count)
      .reduce((a, b) => a + b, 0);
    results.push({
      ...metadata,
      turns: scrubbedTurns.slice(offset, offset + count),
      _entities_found: recordEntities,
    });
    offset += count;
  }
  return results;
}

// --- Upload handler ---

export async function handleUpload(request, env) {
  // Reject oversized requests before reading body
  const contentLength = parseInt(
    request.headers.get("content-length") || "0",
    10
  );
  if (contentLength > MAX_UPLOAD_BYTES) {
    return jsonResponse(
      { error: `Request too large (max ${MAX_UPLOAD_BYTES} bytes)` },
      413
    );
  }

  // Validate API key
  const apiKey = request.headers.get("X-API-Key");
  const user = await validateApiKey(apiKey, env);
  if (!user) {
    await incrementMetric(env, "auth_failures_total");
    return jsonResponse({ error: "Invalid or missing API key" }, 401);
  }

  // Enforce 1-hour cooldown on new keys (configurable via env for testing)
  const cooldownSeconds = parseInt(env.KEY_COOLDOWN_SECONDS || KEY_COOLDOWN_SECONDS, 10);
  if (user.created_at && cooldownSeconds > 0) {
    const age = Date.now() - new Date(user.created_at).getTime();
    if (age < cooldownSeconds * 1000) {
      const remaining = Math.ceil(
        (cooldownSeconds * 1000 - age) / 1000
      );
      await incrementMetric(env, "cooldown_blocks_total");
      return jsonResponse(
        {
          error: `New API key cooldown: ${remaining}s remaining. Try again later.`,
        },
        429
      );
    }
  }

  // Global rate limit
  if (!(await checkGlobalRateLimit(env))) {
    await incrementMetric(env, "global_rate_limited_total");
    return jsonResponse(
      { error: "Service is busy. Please try again shortly." },
      429
    );
  }

  // Per-key rate limit (tier-based)
  const tier = user.tier || 3; // Existing keys without tier get full rate
  if (!(await checkRateLimit(apiKey, env, tier))) {
    return jsonResponse(
      { error: "Rate limit exceeded. Try again later." },
      429
    );
  }

  // Atomic anti-burst cap (native binding) checked BEFORE the expensive body
  // parse / NER / HF work. The tier check above is a raced KV read-then-write;
  // a concurrent burst can pass it en masse before any increment sticks. The
  // native limiter is atomic, so it bounds the burst per key. Falls back to the
  // KV counters alone when the binding isn't configured.
  if (env.UPLOAD_RATE_LIMITER) {
    const id = await apiKeyId(apiKey);
    const { success } = await env.UPLOAD_RATE_LIMITER.limit({ key: id });
    if (!success) {
      await incrementMetric(env, "global_rate_limited_total");
      return jsonResponse(
        { error: "Rate limit exceeded. Try again later." },
        429
      );
    }
  }

  // Parse body with streaming size enforcement for gzip
  let jsonlContent;
  const encoding = request.headers.get("content-encoding") || "";
  if (encoding.includes("gzip")) {
    try {
      const compressed = await request.arrayBuffer();
      const stream = new Response(compressed).body.pipeThrough(
        new DecompressionStream("gzip")
      );
      const reader = stream.getReader();
      const chunks = [];
      let totalBytes = 0;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        totalBytes += value.length;
        if (totalBytes > MAX_UPLOAD_BYTES) {
          reader.cancel();
          return jsonResponse(
            { error: `Request too large (max ${MAX_UPLOAD_BYTES} bytes)` },
            413
          );
        }
        chunks.push(value);
      }
      jsonlContent = new TextDecoder().decode(
        chunks.length === 1
          ? chunks[0]
          : new Uint8Array(
              chunks.reduce((acc, c) => {
                const merged = new Uint8Array(acc.length + c.length);
                merged.set(acc);
                merged.set(c, acc.length);
                return merged;
              }, new Uint8Array(0))
            )
      );
    } catch (err) {
      return jsonResponse({ error: "Invalid gzip data" }, 400);
    }
  } else {
    jsonlContent = await request.text();
    if (new TextEncoder().encode(jsonlContent).length > MAX_UPLOAD_BYTES) {
      return jsonResponse(
        { error: `Request too large (max ${MAX_UPLOAD_BYTES} bytes)` },
        413
      );
    }
  }
  if (!jsonlContent.trim()) {
    await incrementMetric(env, "validation_errors_total");
    return jsonResponse({ error: "Empty request body" }, 400);
  }

  // Content hash dedup — compute hash now, record after successful upload
  // so failed uploads don't permanently block retries.
  const hashHex = await contentHashHex(jsonlContent);
  if (await checkContentHash(hashHex, env)) {
    await incrementMetric(env, "dedup_blocks_total");
    return jsonResponse(
      { error: "Duplicate upload detected" },
      409
    );
  }

  // Validate JSONL structure and content
  const validation = validateJsonl(jsonlContent);
  if (validation.error) {
    if (validation.error.includes("Content blocked")) {
      await incrementMetric(env, "content_blocks_total");
      await decayTier(apiKey, env);
      const category =
        validation.error.match(/\((\w+)\)/)?.[1] || "unknown";
      console.log(`Content blocked: category=${category}`);
    } else if (validation.error.includes("Unscrubbed PII")) {
      await incrementMetric(env, "pii_rejections_total");
      await decayTier(apiKey, env);
      const piiType =
        validation.error.match(/\((\w+)\)/)?.[1] || "unknown";
      console.log(`PII rejected: type=${piiType}`);
    } else {
      await incrementMetric(env, "validation_errors_total");
    }
    return jsonResponse({ error: validation.error }, 422);
  }

  // Server-side NER
  let finalJsonl = jsonlContent;
  const nerUrl = env.NER_SERVICE_URL;
  // Fail closed when NER is unconfigured: without this, an empty NER_SERVICE_URL
  // (a one-line deploy slip) silently ships content with no server-side name
  // scrubbing. A deployer who genuinely wants no server NER must opt in
  // explicitly with ALLOW_NO_NER="true".
  if (!nerUrl && env.ALLOW_NO_NER !== "true") {
    await incrementMetric(env, "ner_errors_total");
    console.error("NER_SERVICE_URL not set and ALLOW_NO_NER!=true — refusing upload");
    return jsonResponse(
      { error: "Server-side scrubbing is not configured. Please retry later." },
      503
    );
  }
  if (nerUrl) {
    try {
      const scrubbed = await scrubViaNer(
        validation.records,
        nerUrl,
        env.NER_API_KEY || ""
      );
      await incrementMetricBy(
        env,
        "ner_entities_scrubbed",
        scrubbed.reduce((sum, r) => sum + (r._entities_found || 0), 0)
      );
      finalJsonl = scrubbed
        .map((r) => {
          const { _entities_found, ...clean } = r;
          return JSON.stringify(clean);
        })
        .join("\n");
    } catch (err) {
      console.error(`NER service error: ${err.message}`);
      await incrementMetric(env, "ner_errors_total");
      const isTurnLimit = err.message.includes("Too many turns");
      return jsonResponse(
        { error: isTurnLimit ? err.message : "NER service unavailable — please retry in ~30s" },
        isTurnLimit ? 422 : 503
      );
    }
  }

  // Forward to HuggingFace
  try {
    const result = await uploadToHuggingFace(finalJsonl, env);
    await recordContentHash(hashHex, env);
    // Only count against rate limits on successful upload
    await incrementRateLimit(apiKey, env);
    await incrementGlobalRateLimit(env);
    await incrementMetric(env, "uploads_total");
    await incrementMetricBy(env, "conversations_total", validation.count);

    // Track contribution for 90-day rollback window
    const keyId = await apiKeyId(apiKey);
    const contribKey = `contrib:${keyId}:${result._filePath}`;
    await env.METRICS.put(contribKey, String(validation.count), {
      expirationTtl: CONTRIBUTION_TTL,
    });

    console.log(`Upload success: ${validation.count} conversations`);
    return jsonResponse({
      ok: true,
      conversations: validation.count,
      commit: result.commitUrl || null,
    });
  } catch (err) {
    await incrementMetric(env, "uploads_failed");
    console.error(`Upload failed: ${err.message}`);
    return jsonResponse({ error: "Upload failed — please retry" }, 502);
  }
}
