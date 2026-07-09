/**
 * Shared helpers: response builders, crypto utilities, metrics, rate limiting.
 */

// User code alphabet: uppercase alpha minus ambiguous I/O
const USER_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ";

// --- Trust tiers ---

const TIER_LIMITS = { 1: 10, 2: 25, 3: 50 };

const GLOBAL_RATE_LIMIT_PER_MINUTE = 100;

// --- Response helpers ---

export function jsonResponse(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

export function htmlResponse(html, status = 200) {
  return new Response(html, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}

// --- Crypto helpers ---

export function generateHex(bytes) {
  const arr = crypto.getRandomValues(new Uint8Array(bytes));
  return Array.from(arr)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export function generateUserCode() {
  const arr = crypto.getRandomValues(new Uint8Array(8));
  let code = "";
  for (let i = 0; i < 8; i++) {
    code += USER_CODE_CHARS[arr[i] % USER_CODE_CHARS.length];
  }
  return code.slice(0, 4) + "-" + code.slice(4);
}

export async function hashIpForRateLimit(ip, date) {
  // SHA-256 truncated to 12 hex chars — consistent within a day, no collisions in practice.
  const data = new TextEncoder().encode(`${ip}:${date}:common-parlance-reg-salt`);
  const hash = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(hash).slice(0, 6)]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

// Opaque, collision-resistant id derived from the FULL API key, for use as a
// KV key-name component (rate buckets, contribution tracking). Hashing the
// whole key — instead of slicing its first 16 chars — keeps a piece of the
// live secret out of the KV keyspace (and out of any dashboard/log that shows
// key names), and removes the prefix-collision risk where two keys sharing a
// 16-char prefix landed in the same bucket. 16 bytes (128 bits) of SHA-256.
export async function apiKeyId(apiKey) {
  const data = new TextEncoder().encode(`${apiKey}:cp-key-id`);
  const hash = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(hash).slice(0, 16)]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

// --- Metrics ---
// Note: KV is eventually consistent (~60s) with no atomic increment.
// These read-then-write counters can lose increments under concurrency.
// Acceptable at launch scale — see recommended_upgrades.md #7 for
// Durable Objects migration path.

export async function incrementMetric(env, key) {
  if (!env.METRICS) return;
  const current = parseInt((await env.METRICS.get(key)) || "0", 10);
  await env.METRICS.put(key, String(current + 1));
}

export async function incrementMetricBy(env, key, amount) {
  if (!env.METRICS) return;
  const current = parseInt((await env.METRICS.get(key)) || "0", 10);
  await env.METRICS.put(key, String(current + amount));
}

export async function getMetrics(env) {
  if (!env.METRICS) return { error: "METRICS KV not configured" };
  const keys = [
    "uploads_total",
    "uploads_failed",
    "conversations_total",
    "content_blocks_total",
    "pii_rejections_total",
    "ner_entities_scrubbed",
    "ner_errors_total",
    "auth_failures_total",
    "validation_errors_total",
    "registrations_total",
    "registrations_rate_limited",
    "turnstile_failures_total",
    "dedup_blocks_total",
    "cooldown_blocks_total",
    "global_rate_limited_total",
  ];
  const metrics = {};
  for (const key of keys) {
    metrics[key] = parseInt((await env.METRICS.get(key)) || "0", 10);
  }
  return metrics;
}

// --- Rate limiting ---
// Same KV eventual-consistency caveat as metrics above. Concurrent
// requests can slip past limits. This is defense-in-depth, not a
// security boundary — real abuse protection is Turnstile + trust tiers (plus
// the native Rate Limiting binding on /register/init; PoW was removed).

export async function checkRateLimit(apiKey, env, tier = 3) {
  if (!env.METRICS) return true;
  const limit = TIER_LIMITS[tier] || TIER_LIMITS[1];
  const hour = new Date().toISOString().slice(0, 13);
  const id = await apiKeyId(apiKey);
  const rateKey = `rate:${id}:${hour}`;
  const current = parseInt((await env.METRICS.get(rateKey)) || "0", 10);
  return current < limit;
}

export async function incrementRateLimit(apiKey, env) {
  if (!env.METRICS) return;
  const hour = new Date().toISOString().slice(0, 13);
  const id = await apiKeyId(apiKey);
  const rateKey = `rate:${id}:${hour}`;
  const current = parseInt((await env.METRICS.get(rateKey)) || "0", 10);
  await env.METRICS.put(rateKey, String(current + 1), { expirationTtl: 7200 });
}

export async function checkGlobalRateLimit(env) {
  if (!env.METRICS) return true;
  const minute = new Date().toISOString().slice(0, 16);
  const key = `global_rate:${minute}`;
  const current = parseInt((await env.METRICS.get(key)) || "0", 10);
  return current < GLOBAL_RATE_LIMIT_PER_MINUTE;
}

export async function incrementGlobalRateLimit(env) {
  if (!env.METRICS) return;
  const minute = new Date().toISOString().slice(0, 16);
  const key = `global_rate:${minute}`;
  const current = parseInt((await env.METRICS.get(key)) || "0", 10);
  await env.METRICS.put(key, String(current + 1), { expirationTtl: 300 });
}

// --- Trust tier management ---

export async function decayTier(apiKey, env) {
  const userData = await env.API_KEYS.get(apiKey);
  if (!userData) return;
  try {
    const user = JSON.parse(userData);
    const currentTier = user.tier || 3;
    if (currentTier > 1) {
      user.tier = 1; // Reset to lowest on any failure
      user.tier_updated = new Date().toISOString();
      await env.API_KEYS.put(apiKey, JSON.stringify(user));
    }
  } catch {
    // Non-JSON metadata, skip
  }
}

// --- API key validation ---

export async function validateApiKey(apiKey, env) {
  if (!apiKey) return null;
  const userData = await env.API_KEYS.get(apiKey);
  if (!userData) return null;
  try {
    return JSON.parse(userData);
  } catch {
    // Fail closed: a key whose stored metadata isn't valid JSON is malformed,
    // not a "legacy" grant. Returning a truthy user here let a corrupt/
    // fat-fingered KV value authenticate as a tier-3 (top-budget) uploader.
    // Reject it instead. (There is no documented bare-string key format.)
    console.error("API key has non-JSON metadata — rejecting");
    return null;
  }
}

// --- Content hash dedup ---

export async function contentHashHex(jsonlContent) {
  const data = new TextEncoder().encode(jsonlContent.trim());
  const hashBuffer = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(hashBuffer)]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export async function checkContentHash(hashHex, env) {
  if (!env.METRICS) return false;
  const existing = await env.METRICS.get(`content_hash:${hashHex}`);
  return !!existing;
}

export async function recordContentHash(hashHex, env) {
  if (!env.METRICS) return;
  await env.METRICS.put(`content_hash:${hashHex}`, "1", {
    expirationTtl: 2592000,
  }); // 30 days
}
