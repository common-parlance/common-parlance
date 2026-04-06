import {
  describe,
  it,
  expect,
  beforeEach,
  afterEach,
  vi,
} from "vitest";
import { SELF, env } from "cloudflare:test";

/**
 * Brute-force PoW solver: find nonce where SHA-256(challenge + nonce) starts
 * with `difficulty` zero hex characters.
 */
async function solvePoW(
  challenge: string,
  difficulty: number
): Promise<number> {
  const prefix = "0".repeat(difficulty);
  for (let nonce = 0; ; nonce++) {
    const data = new TextEncoder().encode(challenge + nonce);
    const hash = await crypto.subtle.digest("SHA-256", data);
    const hex = [...new Uint8Array(hash)]
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
    if (hex.startsWith(prefix)) return nonce;
  }
}

/** Mock Turnstile verification as passing. */
function mockTurnstileSuccess() {
  globalThis.fetch = vi.fn(async () => {
    return new Response(JSON.stringify({ success: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof globalThis.fetch;
}

/** Mock Turnstile verification as failing. */
function mockTurnstileFailure() {
  globalThis.fetch = vi.fn(async () => {
    return new Response(JSON.stringify({ success: false, "error-codes": ["invalid-input-response"] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof globalThis.fetch;
}

/**
 * Helper: run /register/init and return device_code + normalized user_code.
 * Uses a unique IP per call to avoid rate-limit collisions between tests.
 */
let _ipCounter = 0;
async function initRegistration() {
  const ip = `203.0.113.${++_ipCounter}`;
  const initResp = await SELF.fetch("http://localhost/register/init", {
    method: "POST",
    headers: { "CF-Connecting-IP": ip },
  });
  expect(initResp.status).toBe(200);
  const body = await initResp.json() as {
    device_code: string;
    user_code: string;
  };
  return {
    device_code: body.device_code,
    user_code: body.user_code,
    userCodeNorm: body.user_code.replace("-", ""),
    ip,
  };
}

// NOTE: Tests share KV state within this file (rate-limit counters, device
// codes, PoW challenges). Each test uses a unique IP via initRegistration()
// to avoid collisions.

describe("Registration flow — integration", () => {
  let origFetch: typeof globalThis.fetch;

  beforeEach(() => {
    origFetch = globalThis.fetch;
  });

  afterEach(() => {
    globalThis.fetch = origFetch;
  });

  // --- Full registration flow ---

  it("completes full flow: init -> challenge -> solve PoW -> complete -> poll -> get key", { timeout: 30000 }, async () => {
    const { device_code, userCodeNorm, ip } = await initRegistration();

    // Fetch PoW challenge
    const challengeResp = await SELF.fetch(
      `http://localhost/register/challenge/${userCodeNorm}`
    );
    expect(challengeResp.status).toBe(200);
    const { challenge, difficulty } = await challengeResp.json() as {
      challenge: string;
      difficulty: number;
    };

    // Solve PoW
    const nonce = await solvePoW(challenge, difficulty);

    // Complete registration (mock Turnstile as passing)
    mockTurnstileSuccess();
    const completeResp = await SELF.fetch(
      "http://localhost/register/complete",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "CF-Connecting-IP": ip,
        },
        body: JSON.stringify({
          user_code: userCodeNorm,
          turnstile_token: "fake-turnstile-token",
          pow_nonce: nonce,
        }),
      }
    );
    expect(completeResp.status).toBe(200);
    const completeBody = await completeResp.json() as { ok: boolean };
    expect(completeBody.ok).toBe(true);

    // Restore fetch before polling (no outbound needed)
    globalThis.fetch = origFetch;

    // Poll for API key
    const pollResp = await SELF.fetch(
      `http://localhost/register/poll/${device_code}`
    );
    expect(pollResp.status).toBe(200);
    const pollBody = await pollResp.json() as {
      status: string;
      api_key?: string;
    };
    expect(pollBody.status).toBe("complete");
    expect(pollBody.api_key).toBeDefined();
    expect(pollBody.api_key).toMatch(/^cp_live_/);

    // Verify the key exists in KV
    const keyData = await env.API_KEYS.get(pollBody.api_key!);
    expect(keyData).not.toBeNull();
    const parsed = JSON.parse(keyData!);
    expect(parsed.tier).toBe(1);
    expect(parsed.created_at).toBeDefined();
  });

  // --- Poll pending state ---

  it("returns pending status before registration is complete", async () => {
    const { device_code } = await initRegistration();

    const pollResp = await SELF.fetch(
      `http://localhost/register/poll/${device_code}`
    );
    expect(pollResp.status).toBe(200);

    const pollBody = await pollResp.json() as { status: string };
    expect(pollBody.status).toBe("pending");
  });

  // --- Rate limiting on /register/init ---

  it("returns 429 when /register/init rate limit is exceeded", async () => {
    const ip = "198.51.100.99";
    const date = new Date().toISOString().slice(0, 10);
    const ipData = new TextEncoder().encode(
      `${ip}:${date}:common-parlance-reg-salt`
    );
    const hashBuf = await crypto.subtle.digest("SHA-256", ipData);
    const ipHash = [...new Uint8Array(hashBuf).slice(0, 6)]
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");

    const minute = new Date().toISOString().slice(0, 16);
    const initRateKey = `reg_init:${ipHash}:${minute}`;
    await env.METRICS.put(initRateKey, "1000");

    const resp = await SELF.fetch("http://localhost/register/init", {
      method: "POST",
      headers: { "CF-Connecting-IP": ip },
    });

    expect(resp.status).toBe(429);
    const body = await resp.json() as { error: string };
    expect(body.error).toMatch(/Too many requests/);
  });

  // --- Expired device code ---

  it("returns 404 for unknown/expired device code", async () => {
    const resp = await SELF.fetch(
      "http://localhost/register/poll/0000000000000000000000000000dead"
    );
    expect(resp.status).toBe(404);

    const body = await resp.json() as { error: string };
    expect(body.error).toMatch(/Unknown or expired/);
  });

  // --- Invalid PoW ---

  it("returns 400 for invalid proof-of-work solution", async () => {
    const { userCodeNorm, ip } = await initRegistration();

    mockTurnstileSuccess();
    const resp = await SELF.fetch("http://localhost/register/complete", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "CF-Connecting-IP": ip,
      },
      body: JSON.stringify({
        user_code: userCodeNorm,
        turnstile_token: "fake-token",
        pow_nonce: 999999999,
      }),
    });

    expect(resp.status).toBe(400);
    const body = await resp.json() as { error: string };
    expect(body.error).toMatch(/Invalid proof-of-work/);
  });

  // --- Expired PoW challenge ---

  it("returns 400 when PoW challenge is expired/missing", async () => {
    mockTurnstileSuccess();

    const resp = await SELF.fetch("http://localhost/register/complete", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "CF-Connecting-IP": "203.0.113.250",
      },
      body: JSON.stringify({
        user_code: "ZZZZZZZZ",
        turnstile_token: "fake-token",
        pow_nonce: 0,
      }),
    });

    expect(resp.status).toBe(400);
    const body = await resp.json() as { error: string };
    expect(body.error).toMatch(/PoW challenge expired/);
  });

  // --- Turnstile failure ---

  it("returns 403 when Turnstile verification fails", async () => {
    const { userCodeNorm, ip } = await initRegistration();

    mockTurnstileFailure();
    const resp = await SELF.fetch("http://localhost/register/complete", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "CF-Connecting-IP": ip,
      },
      body: JSON.stringify({
        user_code: userCodeNorm,
        turnstile_token: "bad-token",
        pow_nonce: 0,
      }),
    });

    expect(resp.status).toBe(403);
    const body = await resp.json() as { error: string };
    expect(body.error).toMatch(/Verification challenge failed/);
  });
});
