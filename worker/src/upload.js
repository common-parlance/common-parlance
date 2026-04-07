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
} from "./helpers.js";
import { KEY_COOLDOWN_SECONDS } from "./registration.js";

// --- Contribution tracking ---
// Batch-level attribution with auto-expiry for rollback capability.
// Maps API key → batch filenames for 90 days, then auto-deletes.
const CONTRIBUTION_TTL = 90 * 24 * 3600; // 90 days

const MAX_UPLOAD_BYTES = 2 * 1024 * 1024;

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

    if (!record.conversation_id || !Array.isArray(record.turns)) {
      return {
        error: `Line ${i + 1}: missing conversation_id or turns array`,
      };
    }

    // Validate metadata field types to prevent schema drift in the dataset
    if (typeof record.turn_count !== "number" || !Number.isInteger(record.turn_count)) {
      return { error: `Line ${i + 1}: turn_count must be an integer` };
    }
    if (record.language !== null && typeof record.language !== "string") {
      return { error: `Line ${i + 1}: language must be a string or null` };
    }
    if (record.quality_signals === null || typeof record.quality_signals !== "object" || Array.isArray(record.quality_signals)) {
      return { error: `Line ${i + 1}: quality_signals must be an object` };
    }
    if (typeof record.ner_scrubbed !== "boolean") {
      return { error: `Line ${i + 1}: ner_scrubbed must be a boolean` };
    }

    for (const turn of record.turns) {
      if (!turn.role || !turn.content) {
        return { error: `Line ${i + 1}: turn missing role or content` };
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
    const prefix = apiKey.slice(0, 16);
    const contribKey = `contrib:${prefix}:${result._filePath}`;
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
