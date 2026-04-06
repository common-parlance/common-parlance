/**
 * Common Parlance Upload Proxy — Entry point and routing.
 *
 * Cloudflare Worker that sits between clients and HuggingFace.
 * - Anonymous API key self-registration (device auth flow + Turnstile)
 * - Validates client API keys (stored in KV)
 * - Runs server-side content checks on uploaded JSONL
 * - Forwards valid uploads to HuggingFace using our credentials
 * - Tracks metrics (uploads, blocks, errors) in KV
 * - No secrets in the client package
 *
 * Env bindings:
 *   API_KEYS  — KV namespace (key = api_key, value = JSON user metadata)
 *   METRICS   — KV namespace (counters, device codes, rate limits)
 *   HF_TOKEN  — Secret: HuggingFace write token for the dataset repo
 *   HF_REPO   — Var: target dataset repo (e.g. "common-parlance/conversations")
 *   TURNSTILE_SITE_KEY — Var: Cloudflare Turnstile site key (public)
 *   TURNSTILE_SECRET   — Secret: Cloudflare Turnstile secret key
 */

import { jsonResponse, getMetrics, validateApiKey } from "./helpers.js";
import {
  handleRegisterInit,
  handleRegisterPage,
  handleRegisterComplete,
  handleRegisterPoll,
  handlePowChallenge,
  handlePublicStats,
} from "./registration.js";
import { handleUpload } from "./upload.js";

// --- Admin handlers ---

async function handleHealth(env) {
  if (!env.HF_REPO) {
    console.warn("HF_REPO not set — uploads will go to default dataset");
  }
  return jsonResponse({ ok: true });
}

async function handleMetrics(request, env) {
  const apiKey = request.headers.get("X-API-Key");
  const user = await validateApiKey(apiKey, env);
  if (!user || !user.admin) {
    return jsonResponse({ error: "Admin access required" }, 403);
  }
  const metrics = await getMetrics(env);
  return jsonResponse(metrics);
}

async function handleContributions(request, env) {
  // Admin-only: list batch files uploaded by a specific API key
  const adminKey = request.headers.get("X-API-Key");
  const admin = await validateApiKey(adminKey, env);
  if (!admin || !admin.admin) {
    return jsonResponse({ error: "Admin access required" }, 403);
  }

  const url = new URL(request.url);
  const targetKey = url.searchParams.get("key");
  if (!targetKey) {
    return jsonResponse({ error: "Missing ?key= parameter" }, 400);
  }

  const prefix = targetKey.slice(0, 16);
  // List all contribution entries for this key
  const list = await env.METRICS.list({ prefix: `contrib:${prefix}:` });
  const files = list.keys.map((k) => {
    const filePath = k.name.replace(`contrib:${prefix}:`, "");
    return filePath;
  });

  return jsonResponse({ key_prefix: prefix, files, count: files.length });
}

async function handlePurge(request, env) {
  // Admin-only: delete batch files from HuggingFace for a specific API key
  const adminKey = request.headers.get("X-API-Key");
  const admin = await validateApiKey(adminKey, env);
  if (!admin || !admin.admin) {
    return jsonResponse({ error: "Admin access required" }, 403);
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse({ error: "Invalid request body" }, 400);
  }

  const { key, files } = body;
  if (!key || !Array.isArray(files) || files.length === 0) {
    return jsonResponse(
      { error: "Provide key and files array" },
      400
    );
  }

  const repo = env.HF_REPO || "common-parlance/conversations";
  const prefix = key.slice(0, 16);
  const deleted = [];
  const failed = [];

  // Validate file paths to prevent path traversal
  const VALID_BATCH_PATH = /^data\/batch_\d{4}-\d{2}-\d{2}_[a-z0-9]+\.jsonl$/;
  for (const filePath of files) {
    if (!VALID_BATCH_PATH.test(filePath)) {
      failed.push({ file: filePath, error: "Invalid file path" });
      continue;
    }
    try {
      // Delete file from HuggingFace via commit API
      const deleteOp = JSON.stringify({
        key: "header",
        value: {
          summary: `Remove batch (policy enforcement)`,
          description: "Automated removal via admin purge",
        },
      }) + "\n" + JSON.stringify({
        key: "deletedFile",
        value: { path: filePath },
      });

      const hfApiBase = (env.HF_API_BASE || "https://huggingface.co").replace(/\/$/, "");
      const response = await fetch(
        `${hfApiBase}/api/datasets/${repo}/commit/main`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${env.HF_TOKEN}`,
            "Content-Type": "application/x-ndjson",
          },
          body: deleteOp,
        }
      );

      if (response.ok) {
        deleted.push(filePath);
        // Clean up contribution tracking
        await env.METRICS.delete(`contrib:${prefix}:${filePath}`);
      } else {
        failed.push({ file: filePath, status: response.status });
      }
    } catch (err) {
      console.error(`Purge error for ${filePath}: ${err.message}`);
      failed.push({ file: filePath, error: "internal error" });
    }
  }

  console.log(
    `Purge for key ${prefix}: ${deleted.length} deleted, ${failed.length} failed`
  );

  return jsonResponse({ deleted, failed });
}

// --- Entry point ---

export default {
  async fetch(request, env) {
    try {
      const url = new URL(request.url);

      // Registration routes
      if (url.pathname === "/register/init" && request.method === "POST") {
        return handleRegisterInit(request, env);
      }
      if (url.pathname === "/register" && request.method === "GET") {
        return handleRegisterPage(env);
      }
      if (url.pathname === "/register/complete" && request.method === "POST") {
        return handleRegisterComplete(request, env);
      }
      if (
        url.pathname.startsWith("/register/challenge/") &&
        request.method === "GET"
      ) {
        const code = url.pathname.split("/register/challenge/")[1];
        if (code) return handlePowChallenge(code, env);
      }
      if (
        url.pathname.startsWith("/register/poll/") &&
        request.method === "GET"
      ) {
        const deviceCode = url.pathname.split("/register/poll/")[1];
        if (deviceCode) {
          return handleRegisterPoll(deviceCode, env);
        }
      }

      // Core routes
      if (url.pathname === "/health" && request.method === "GET") {
        return handleHealth(env);
      }
      if (url.pathname === "/upload" && request.method === "POST") {
        return handleUpload(request, env);
      }
      if (url.pathname === "/metrics" && request.method === "GET") {
        return handleMetrics(request, env);
      }
      if (url.pathname === "/stats" && request.method === "GET") {
        return handlePublicStats(env);
      }
      if (url.pathname === "/admin/contributions" && request.method === "GET") {
        return handleContributions(request, env);
      }
      if (url.pathname === "/admin/purge" && request.method === "POST") {
        return handlePurge(request, env);
      }

      return jsonResponse({ error: "Not found" }, 404);
    } catch (err) {
      console.error(`Unhandled error: ${err.message}`);
      return jsonResponse({ error: "Internal server error" }, 500);
    }
  },
};
