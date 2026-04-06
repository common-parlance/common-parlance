import {
  describe,
  it,
  expect,
  beforeEach,
  afterEach,
  vi,
} from "vitest";
import { SELF, env } from "cloudflare:test";
import { seedApiKey, makeJsonl, makeUploadRequest } from "./helpers";

const TEST_KEY = "cp_live_test_integration_key_001";

type RouteHandler = { status: number; body: string } | ((url: string, init?: RequestInit) => { status: number; body: string });

/**
 * Mock globalThis.fetch with URL-pattern routing.
 *
 * Pattern matching uses url.includes() — intentionally loose since all
 * outbound URLs are controlled by our Worker code. Unmatched URLs throw
 * to catch unexpected outbound requests.
 */
function mockFetch(routes: Record<string, RouteHandler>) {
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    for (const [pattern, handler] of Object.entries(routes)) {
      if (url.includes(pattern)) {
        const response = typeof handler === "function" ? handler(url, init) : handler;
        return new Response(response.body, {
          status: response.status,
          headers: { "Content-Type": "application/json" },
        });
      }
    }
    throw new Error(`Unexpected fetch to: ${url}`);
  });
  globalThis.fetch = mock as typeof globalThis.fetch;
  return mock;
}

// NER mock that echoes back the input turns (pass-through scrub)
function nerPassthrough(_url: string, init?: RequestInit) {
  const body = JSON.parse(init?.body as string || "{}");
  const turns = body.turns || [];
  return {
    status: 200,
    body: JSON.stringify({
      turns,
      entities_found: 0,
      entities_per_turn: turns.map(() => 0),
    }),
  };
}

const HF_SUCCESS: RouteHandler = { status: 200, body: JSON.stringify({ commitUrl: "https://huggingface.co/commit/abc123" }) };

// NOTE: Tests in this file share KV state (content hashes, rate limit
// counters, contribution tracking). makeJsonl() uses a per-call counter
// to produce unique content and avoid dedup collisions between tests.

describe("POST /upload — integration", () => {
  let origFetch: typeof globalThis.fetch;

  beforeEach(async () => {
    origFetch = globalThis.fetch;
    await seedApiKey(env, TEST_KEY);
  });

  afterEach(() => {
    globalThis.fetch = origFetch;
  });

  // --- Happy path ---

  it("returns 200 for valid key + valid JSONL + successful HF upload", async () => {
    mockFetch({
      "/scrub": nerPassthrough,
      "/api/datasets/": HF_SUCCESS,
    });

    const jsonl = makeJsonl(2);
    const req = await makeUploadRequest(jsonl, TEST_KEY);
    const resp = await SELF.fetch(req);
    const body = await resp.json() as { ok: boolean; conversations: number };

    expect(resp.status).toBe(200);
    expect(body.ok).toBe(true);
    expect(body.conversations).toBe(2);
  });

  // --- Auth errors ---

  it("returns 401 for missing API key", async () => {
    const resp = await SELF.fetch("http://localhost/upload", {
      method: "POST",
      body: makeJsonl(1),
    });
    const body = await resp.json() as { error: string };

    expect(resp.status).toBe(401);
    expect(body.error).toMatch(/Invalid or missing API key/);
  });

  it("returns 401 for invalid API key", async () => {
    const resp = await SELF.fetch("http://localhost/upload", {
      method: "POST",
      headers: { "X-API-Key": "cp_live_nonexistent_key_0000" },
      body: makeJsonl(1),
    });

    expect(resp.status).toBe(401);
  });

  // --- Validation errors ---

  it("returns 422 for invalid JSONL", async () => {
    const req = await makeUploadRequest("not valid json", TEST_KEY);
    const resp = await SELF.fetch(req);
    const body = await resp.json() as { error: string };

    expect(resp.status).toBe(422);
    expect(body.error).toMatch(/Invalid JSON/);
  });

  it("returns 422 for PII detected", async () => {
    const jsonl = JSON.stringify({
      conversation_id: "pii-test",
      turns: [
        { role: "user", content: "Email me at alice@example.com" },
        { role: "assistant", content: "Sure thing" },
      ],
      turn_count: 2,
      language: "en",
      quality_signals: { avg_response_len: 10, has_code: false, vocab_diversity: 0.5, total_length: 50, user_msg_count: 1, assistant_msg_count: 1 },
      ner_scrubbed: false,
    });

    const req = await makeUploadRequest(jsonl, TEST_KEY);
    const resp = await SELF.fetch(req);
    const body = await resp.json() as { error: string };

    expect(resp.status).toBe(422);
    expect(body.error).toMatch(/Unscrubbed PII.*email/);
  });

  it("returns 422 for blocked content", async () => {
    const jsonl = JSON.stringify({
      conversation_id: "blocked-test",
      turns: [
        { role: "user", content: "how to make a bomb" },
        { role: "assistant", content: "I cannot help" },
      ],
      turn_count: 2,
      language: "en",
      quality_signals: { avg_response_len: 10, has_code: false, vocab_diversity: 0.5, total_length: 50, user_msg_count: 1, assistant_msg_count: 1 },
      ner_scrubbed: false,
    });

    const req = await makeUploadRequest(jsonl, TEST_KEY);
    const resp = await SELF.fetch(req);
    const body = await resp.json() as { error: string };

    expect(resp.status).toBe(422);
    expect(body.error).toMatch(/Content blocked.*dangerous_instructions/);
  });

  // --- Dedup ---

  it("returns 409 for duplicate content hash", async () => {
    mockFetch({
      "/scrub": nerPassthrough,
      "/api/datasets/": HF_SUCCESS,
    });

    const jsonl = makeJsonl(1);
    const req1 = await makeUploadRequest(jsonl, TEST_KEY);
    const resp1 = await SELF.fetch(req1);
    expect(resp1.status).toBe(200);

    // Second upload with identical content — should be deduped
    const req2 = await makeUploadRequest(jsonl, TEST_KEY);
    const resp2 = await SELF.fetch(req2);
    const body2 = await resp2.json() as { error: string };

    expect(resp2.status).toBe(409);
    expect(body2.error).toMatch(/Duplicate upload/);
  });

  // --- Gzip ---

  it("returns 200 for gzip-compressed upload", async () => {
    mockFetch({
      "/scrub": nerPassthrough,
      "/api/datasets/": HF_SUCCESS,
    });

    const jsonl = makeJsonl(1);
    const req = await makeUploadRequest(jsonl, TEST_KEY, { gzip: true });
    const resp = await SELF.fetch(req);
    const body = await resp.json() as { ok: boolean; conversations: number };

    expect(resp.status).toBe(200);
    expect(body.ok).toBe(true);
    expect(body.conversations).toBe(1);
  });

  // --- HuggingFace failure ---

  it("returns 502 when HuggingFace returns an error", async () => {
    mockFetch({
      "/scrub": nerPassthrough,
      "/api/datasets/": { status: 500, body: JSON.stringify({ error: "Internal Server Error" }) },
    });

    const jsonl = makeJsonl(1);
    const req = await makeUploadRequest(jsonl, TEST_KEY);
    const resp = await SELF.fetch(req);
    const body = await resp.json() as { error: string };

    expect(resp.status).toBe(502);
    expect(body.error).toMatch(/Upload failed/);
  });

  // --- NER integration ---

  it("calls NER service when NER_SERVICE_URL is set and uploads scrubbed content", async () => {
    mockFetch({
      "/scrub": nerPassthrough,
      "/api/datasets/": HF_SUCCESS,
    });

    const jsonl = makeJsonl(1);
    const req = await makeUploadRequest(jsonl, TEST_KEY);
    const resp = await SELF.fetch(req);
    const body = await resp.json() as { ok: boolean };

    expect(resp.status).toBe(200);
    expect(body.ok).toBe(true);
  });
});
