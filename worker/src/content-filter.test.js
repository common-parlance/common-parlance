import { describe, it, expect } from "vitest";
import { checkPii, checkContent } from "./content-filter.js";

// --- checkPii ---

describe("checkPii", () => {
  it("detects email addresses", () => {
    expect(checkPii("contact me at alice@example.com")).toBe("email");
  });

  it("detects SSNs", () => {
    expect(checkPii("my SSN is 123-45-6789")).toBe("ssn");
  });

  it("ignores invalid SSNs (000 prefix)", () => {
    expect(checkPii("number 000-12-3456")).toBeNull();
  });

  it("detects US phone numbers", () => {
    expect(checkPii("call me at 555-123-4567")).toBe("phone");
  });

  it("detects phone with country code", () => {
    expect(checkPii("call +1-555-123-4567")).toBe("phone");
  });

  it("detects IPv4 addresses", () => {
    expect(checkPii("server at 192.168.1.100")).toBe("ip");
  });

  it("detects macOS file paths", () => {
    expect(checkPii("file at /Users/alice/Documents")).toBe("filepath");
  });

  it("detects Linux home paths", () => {
    expect(checkPii("file at /home/alice/.config")).toBe("filepath");
  });

  it("detects Windows paths", () => {
    expect(checkPii("file at C:\\Users\\alice\\Desktop")).toBe("filepath");
  });

  it("detects OpenAI API keys", () => {
    expect(checkPii("key: sk-abcdefghijklmnopqrstuvwxyz1234567890")).toBe(
      "api_key"
    );
  });

  it("detects GitHub PATs", () => {
    expect(
      checkPii("token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijkl")
    ).toBe("api_key");
  });

  it("detects AWS access keys", () => {
    expect(checkPii("key AKIAIOSFODNN7EXAMPLE")).toBe("api_key");
  });

  it("detects Slack tokens", () => {
    expect(
      checkPii("token xoxb-abcdefABCDEF-ghijklmnopqrstuv")
    ).toBe("api_key");
  });

  it("detects private keys", () => {
    expect(checkPii("-----BEGIN RSA PRIVATE KEY-----")).toBe("private_key");
  });

  it("detects connection strings", () => {
    expect(checkPii("postgres://user:pass@host:5432/db")).toBe(
      "connection_string"
    );
  });

  it("detects bearer tokens", () => {
    expect(checkPii("Authorization: Bearer abc123.def456.ghi789")).toBe(
      "bearer_token"
    );
  });

  it("detects JWTs", () => {
    expect(
      checkPii("eyJhbGciOiJIUzI1NiIsInR5cCI6.eyJzdWIiOiIxMjM0NTY3ODkw")
    ).toBe("jwt");
  });

  // --- Allowlist: scrubbed placeholders should not trigger ---

  it("ignores [EMAIL] placeholder", () => {
    expect(checkPii("contact me at [EMAIL]")).toBeNull();
  });

  it("ignores [PHONE] placeholder", () => {
    expect(checkPii("call me at [PHONE]")).toBeNull();
  });

  it("ignores [PATH] placeholder", () => {
    expect(checkPii("file at [PATH]")).toBeNull();
  });

  it("ignores [SECRET] placeholder", () => {
    expect(checkPii("key is [SECRET]")).toBeNull();
  });

  it("ignores [URL:domain] dynamic placeholder", () => {
    expect(checkPii("visit [URL:example.com]")).toBeNull();
  });

  // --- Clean text ---

  it("returns null for clean text", () => {
    expect(checkPii("Hello, how can I help you today?")).toBeNull();
  });

  it("returns null for code without PII", () => {
    expect(checkPii("const x = [1, 2, 3]; console.log(x);")).toBeNull();
  });

  // --- Unicode evasion ---

  it("detects email with zero-width characters", () => {
    expect(checkPii("alice\u200B@example.com")).toBe("email");
  });
});

// --- checkContent ---

describe("checkContent", () => {
  it("blocks CSAM content", () => {
    expect(checkContent("child exploitation material")).toBe("csam_indicator");
  });

  it("blocks dangerous instructions", () => {
    expect(checkContent("how to make a bomb")).toBe("dangerous_instructions");
  });

  it("blocks synthesis instructions", () => {
    expect(checkContent("synthesize methamphetamine")).toBe(
      "dangerous_instructions"
    );
  });

  it("returns null for clean text", () => {
    expect(checkContent("Tell me about Python programming")).toBeNull();
  });

  // --- Leetspeak evasion ---

  it("detects leetspeak evasion of CSAM terms", () => {
    expect(checkContent("ch!ld p0rn")).toBe("csam_indicator");
  });

  // --- Unicode evasion ---

  it("detects CSAM with zero-width characters inserted", () => {
    expect(checkContent("child\u200B porn")).toBe("csam_indicator");
  });
});
