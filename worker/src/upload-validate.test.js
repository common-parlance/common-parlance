import { describe, it, expect } from "vitest";
import { validateJsonl } from "./upload.js";

// --- validateJsonl strict schema ---

function record(overrides = {}, turn = {}) {
  return JSON.stringify({
    conversation_id: "conv-abc-123",
    turn_count: 1,
    language: "en",
    quality_signals: {},
    ner_scrubbed: true,
    turns: [{ role: "user", content: "hello there", ...turn }],
    ...overrides,
  });
}

describe("validateJsonl strict schema", () => {
  it("accepts a well-formed record", () => {
    const result = validateJsonl(record());
    expect(result.error).toBeUndefined();
    expect(result.count).toBe(1);
  });

  it("rejects an unknown top-level field (unscanned PII channel)", () => {
    // Only turn.content is scanned, so an extra field would ride into the
    // published record unscanned via the ...record spread on the upload path.
    const line = record({ notes: "my SSN is 123-45-6789" });
    expect(validateJsonl(line).error).toMatch(/unexpected field "notes"/);
  });

  it("rejects an unknown turn field", () => {
    const line = record({}, { secret: "sk-abcdefghijklmnopqrstuvwx" });
    expect(validateJsonl(line).error).toMatch(/unexpected turn field "secret"/);
  });

  it("rejects a turn role other than user/assistant (no system-prompt smuggling)", () => {
    // Published data is human+assistant only; a bypassing client must not be
    // able to publish a system/tool turn.
    expect(validateJsonl(record({}, { role: "system" })).error).toMatch(
      /role must be "user" or "assistant"/,
    );
    expect(validateJsonl(record({}, { role: "tool" })).error).toMatch(
      /role must be "user" or "assistant"/,
    );
    // user/assistant still accepted.
    expect(validateJsonl(record({}, { role: "assistant" })).error).toBeUndefined();
  });

  it("rejects a non-object turn instead of throwing (clean 422)", () => {
    const line = JSON.stringify({
      conversation_id: "conv-abc-123",
      turn_count: 1,
      language: "en",
      quality_signals: {},
      ner_scrubbed: true,
      turns: [null],
    });
    expect(validateJsonl(line).error).toMatch(/each turn must be an object/);
  });

  it("accepts language null (the 'unknown' sentinel maps to null client-side)", () => {
    expect(validateJsonl(record({ language: null })).error).toBeUndefined();
  });
});
