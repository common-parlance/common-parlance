import {
  describe,
  it,
  expect,
  beforeEach,
  afterEach,
  vi,
} from "vitest";
import { SELF, env } from "cloudflare:test";

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

  it("completes full flow: init -> complete -> poll -> get key", async () => {
    const { device_code, userCodeNorm, ip } = await initRegistration();

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

  it("returns 429 once the per-IP /register/init limit is exceeded", async () => {
    // Exercises the native rate-limiter binding (REG_INIT_RATE_LIMITER,
    // limit 10 / 60s in wrangler.toml). Same IP for every request so they all
    // land in one bucket; the burst eventually trips the limit.
    const ip = "198.51.100.99";
    const statuses: number[] = [];
    for (let i = 0; i < 20; i++) {
      const resp = await SELF.fetch("http://localhost/register/init", {
        method: "POST",
        headers: { "CF-Connecting-IP": ip },
      });
      statuses.push(resp.status);
    }
    expect(statuses[0]).toBe(200);
    expect(statuses).toContain(429);
    // Once tripped it stays tripped within the window.
    expect(statuses[statuses.length - 1]).toBe(429);
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

  // --- Unknown user code ---

  it("returns 404 for an unknown/expired user code (Turnstile passed)", async () => {
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
      }),
    });

    expect(resp.status).toBe(404);
    const body = await resp.json() as { error: string };
    expect(body.error).toMatch(/Code expired or invalid/);
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
      }),
    });

    expect(resp.status).toBe(403);
    const body = await resp.json() as { error: string };
    expect(body.error).toMatch(/Verification challenge failed/);
  });
});
