import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  jsonResponse,
  htmlResponse,
  generateHex,
  generateUserCode,
  checkRateLimit,
  checkGlobalRateLimit,
  decayTier,
  validateApiKey,
  contentHashHex,
  checkContentHash,
  recordContentHash,
} from "./helpers.js";

// --- Response helpers ---

describe("jsonResponse", () => {
  it("returns JSON with correct content type", async () => {
    const res = jsonResponse({ ok: true });
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("application/json");
    expect(await res.json()).toEqual({ ok: true });
  });

  it("uses custom status code", () => {
    const res = jsonResponse({ error: "nope" }, 400);
    expect(res.status).toBe(400);
  });
});

describe("htmlResponse", () => {
  it("returns HTML with correct content type", async () => {
    const res = htmlResponse("<p>hi</p>");
    expect(res.headers.get("Content-Type")).toBe("text/html; charset=utf-8");
    expect(await res.text()).toBe("<p>hi</p>");
  });
});

// --- Crypto helpers ---

describe("generateHex", () => {
  it("returns correct length hex string", () => {
    const hex = generateHex(16);
    expect(hex).toMatch(/^[0-9a-f]{32}$/);
  });

  it("returns different values on each call", () => {
    expect(generateHex(8)).not.toBe(generateHex(8));
  });
});

describe("generateUserCode", () => {
  it("returns XXXX-XXXX format", () => {
    const code = generateUserCode();
    expect(code).toMatch(/^[A-Z]{4}-[A-Z]{4}$/);
  });

  it("excludes ambiguous characters I and O", () => {
    // Generate many codes and check none contain I or O
    for (let i = 0; i < 100; i++) {
      const code = generateUserCode();
      expect(code).not.toMatch(/[IO]/);
    }
  });
});

// --- Content hash ---

describe("content hash", () => {
  it("produces deterministic hex string", async () => {
    const hash1 = await contentHashHex("hello world");
    const hash2 = await contentHashHex("hello world");
    expect(hash1).toBe(hash2);
    expect(hash1).toMatch(/^[0-9a-f]{64}$/);
  });

  it("trims whitespace before hashing", async () => {
    const hash1 = await contentHashHex("hello ");
    const hash2 = await contentHashHex(" hello");
    // Both trim to "hello"
    expect(hash1).toBe(hash2);
  });

  it("produces different hashes for different content", async () => {
    const hash1 = await contentHashHex("hello");
    const hash2 = await contentHashHex("world");
    expect(hash1).not.toBe(hash2);
  });

  it("checkContentHash returns false when not recorded", async () => {
    const env = { METRICS: { get: vi.fn().mockResolvedValue(null) } };
    expect(await checkContentHash("abc123", env)).toBe(false);
  });

  it("checkContentHash returns true when recorded", async () => {
    const env = { METRICS: { get: vi.fn().mockResolvedValue("1") } };
    expect(await checkContentHash("abc123", env)).toBe(true);
  });

  it("recordContentHash stores with 30-day TTL", async () => {
    const put = vi.fn();
    const env = { METRICS: { put } };
    await recordContentHash("abc123", env);
    expect(put).toHaveBeenCalledWith("content_hash:abc123", "1", {
      expirationTtl: 2592000,
    });
  });
});

// --- Rate limiting ---

describe("checkRateLimit", () => {
  let env;
  let store;

  beforeEach(() => {
    store = {};
    env = {
      METRICS: {
        get: vi.fn((key) => Promise.resolve(store[key] || null)),
        put: vi.fn((key, val) => {
          store[key] = val;
          return Promise.resolve();
        }),
      },
    };
  });

  it("allows first request", async () => {
    expect(await checkRateLimit("key123", env, 1)).toBe(true);
  });

  it("blocks after tier 1 limit (10)", async () => {
    for (let i = 0; i < 10; i++) {
      expect(await checkRateLimit("key123", env, 1)).toBe(true);
    }
    expect(await checkRateLimit("key123", env, 1)).toBe(false);
  });

  it("allows more requests for higher tiers", async () => {
    for (let i = 0; i < 25; i++) {
      await checkRateLimit("key123", env, 2);
    }
    expect(await checkRateLimit("key123", env, 2)).toBe(false);
  });

  it("returns true when METRICS is not configured", async () => {
    expect(await checkRateLimit("key123", {}, 1)).toBe(true);
  });
});

describe("checkGlobalRateLimit", () => {
  it("allows requests under limit", async () => {
    const env = {
      METRICS: {
        get: vi.fn().mockResolvedValue("50"),
        put: vi.fn(),
      },
    };
    expect(await checkGlobalRateLimit(env)).toBe(true);
  });

  it("blocks at 100 requests per minute", async () => {
    const env = {
      METRICS: {
        get: vi.fn().mockResolvedValue("100"),
        put: vi.fn(),
      },
    };
    expect(await checkGlobalRateLimit(env)).toBe(false);
  });
});

// --- Trust tier ---

describe("decayTier", () => {
  it("resets tier to 1", async () => {
    const stored = { tier: 3, created_at: "2025-01-01" };
    let updated;
    const env = {
      API_KEYS: {
        get: vi.fn().mockResolvedValue(JSON.stringify(stored)),
        put: vi.fn((key, val) => {
          updated = JSON.parse(val);
        }),
      },
    };
    await decayTier("testkey", env);
    expect(updated.tier).toBe(1);
    expect(updated.tier_updated).toBeDefined();
  });

  it("does nothing for missing key", async () => {
    const env = { API_KEYS: { get: vi.fn().mockResolvedValue(null) } };
    await decayTier("testkey", env);
    // No error thrown
  });

  it("does nothing for already tier 1", async () => {
    const stored = { tier: 1 };
    const env = {
      API_KEYS: {
        get: vi.fn().mockResolvedValue(JSON.stringify(stored)),
        put: vi.fn(),
      },
    };
    await decayTier("testkey", env);
    expect(env.API_KEYS.put).not.toHaveBeenCalled();
  });
});

// --- API key validation ---

describe("validateApiKey", () => {
  it("returns null for missing key", async () => {
    const env = { API_KEYS: { get: vi.fn() } };
    expect(await validateApiKey(null, env)).toBeNull();
  });

  it("returns null for unknown key", async () => {
    const env = { API_KEYS: { get: vi.fn().mockResolvedValue(null) } };
    expect(await validateApiKey("badkey", env)).toBeNull();
  });

  it("returns parsed user data for valid key", async () => {
    const user = { tier: 3, created_at: "2025-01-01" };
    const env = {
      API_KEYS: { get: vi.fn().mockResolvedValue(JSON.stringify(user)) },
    };
    const result = await validateApiKey("goodkey", env);
    expect(result).toEqual(user);
  });

  it("returns fallback for non-JSON metadata", async () => {
    const env = { API_KEYS: { get: vi.fn().mockResolvedValue("not-json") } };
    const result = await validateApiKey("legacykey", env);
    expect(result).toEqual({ valid: true });
  });
});
