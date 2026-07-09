import { describe, it, expect } from "vitest";
import { validateJsonl } from "./upload.js";

function line(id, turns, overrides = {}) {
  return JSON.stringify({
    conversation_id: id,
    turns,
    turn_count: turns.length,
    language: "en",
    quality_signals: { avg_response_len: 10, has_code: false, vocab_diversity: 0.5, total_length: 20, user_msg_count: 1, assistant_msg_count: 1 },
    ner_scrubbed: false,
    ...overrides,
  });
}

function turn(role, content) {
  return { role, content };
}

describe("validateJsonl", () => {
  it("accepts valid single-record JSONL", () => {
    const jsonl = line("conv-1", [
      turn("user", "Hello"),
      turn("assistant", "Hi there"),
    ]);
    const result = validateJsonl(jsonl);
    expect(result.error).toBeUndefined();
    expect(result.count).toBe(1);
    expect(result.records[0].conversation_id).toBe("conv-1");
  });

  it("accepts multi-record JSONL", () => {
    const jsonl = [
      line("conv-1", [turn("user", "A"), turn("assistant", "B")]),
      line("conv-2", [turn("user", "C"), turn("assistant", "D")]),
    ].join("\n");
    const result = validateJsonl(jsonl);
    expect(result.count).toBe(2);
  });

  it("rejects malformed JSON", () => {
    const result = validateJsonl("not json");
    expect(result.error).toMatch(/Invalid JSON on line 1/);
  });

  it("rejects malformed JSON on second line", () => {
    const jsonl = line("conv-1", [turn("user", "A"), turn("assistant", "B")]) +
      "\n{bad json";
    const result = validateJsonl(jsonl);
    expect(result.error).toMatch(/Invalid JSON on line 2/);
  });

  it("rejects missing conversation_id", () => {
    const jsonl = JSON.stringify({ turns: [turn("user", "Hi")] });
    const result = validateJsonl(jsonl);
    expect(result.error).toMatch(/conversation_id must match/);
  });

  it("rejects missing turns array", () => {
    const jsonl = JSON.stringify({ conversation_id: "x" });
    const result = validateJsonl(jsonl);
    expect(result.error).toMatch(/missing turns array/);
  });

  it("rejects a conversation_id with control chars (log injection)", () => {
    const jsonl = line("ok\ninjected log line", [
      turn("user", "Hi"),
      turn("assistant", "Hello"),
    ]);
    const result = validateJsonl(jsonl);
    expect(result.error).toMatch(/conversation_id must match/);
  });

  it("rejects an over-long conversation_id", () => {
    const jsonl = line("a".repeat(129), [
      turn("user", "Hi"),
      turn("assistant", "Hello"),
    ]);
    const result = validateJsonl(jsonl);
    expect(result.error).toMatch(/conversation_id must match/);
  });

  it("accepts a uuid4 conversation_id", () => {
    const jsonl = line("550e8400-e29b-41d4-a716-446655440000", [
      turn("user", "Hi"),
      turn("assistant", "Hello"),
    ]);
    const result = validateJsonl(jsonl);
    expect(result.error).toBeUndefined();
  });

  // --- Metadata validation ---

  it("rejects non-integer turn_count", () => {
    const jsonl = line("x", [turn("user", "Hi"), turn("assistant", "Hello")], { turn_count: "two" });
    const result = validateJsonl(jsonl);
    expect(result.error).toMatch(/turn_count must be an integer/);
  });

  it("rejects missing turn_count", () => {
    const jsonl = JSON.stringify({
      conversation_id: "x",
      turns: [turn("user", "Hi"), turn("assistant", "Hello")],
      language: "en",
      quality_signals: {},
      ner_scrubbed: false,
    });
    const result = validateJsonl(jsonl);
    expect(result.error).toMatch(/turn_count must be an integer/);
  });

  it("rejects non-string language", () => {
    const jsonl = line("x", [turn("user", "Hi"), turn("assistant", "Hello")], { language: 42 });
    const result = validateJsonl(jsonl);
    expect(result.error).toMatch(/language must be a BCP47-style code or null/);
  });

  it("rejects PII smuggled in the language field", () => {
    const jsonl = line("x", [turn("user", "Hi"), turn("assistant", "Hello")], {
      language: "my SSN is 123-45-6789",
    });
    expect(validateJsonl(jsonl).error).toMatch(/language must be a BCP47-style/);
  });

  it("accepts a region-tagged language code", () => {
    const jsonl = line("x", [turn("user", "Hi"), turn("assistant", "Hello")], {
      language: "zh-cn",
    });
    expect(validateJsonl(jsonl).error).toBeUndefined();
  });

  it("rejects PII smuggled in a quality_signals value", () => {
    const jsonl = line("x", [turn("user", "Hi"), turn("assistant", "Hello")], {
      quality_signals: { note: "email bob@evil.com", total_length: 20 },
    });
    expect(validateJsonl(jsonl).error).toMatch(/quality_signals values must be/);
  });

  it("rejects PII smuggled in a turn role", () => {
    const jsonl = line("x", [
      turn("my SSN is 123-45-6789", "Hi"),
      turn("assistant", "Hello"),
    ]);
    expect(validateJsonl(jsonl).error).toMatch(/role must be "user" or "assistant"/);
  });

  it("accepts null language", () => {
    const jsonl = line("x", [turn("user", "Hi"), turn("assistant", "Hello")], { language: null });
    const result = validateJsonl(jsonl);
    expect(result.error).toBeUndefined();
  });

  it("rejects missing quality_signals", () => {
    const jsonl = JSON.stringify({
      conversation_id: "x",
      turns: [turn("user", "Hi"), turn("assistant", "Hello")],
      turn_count: 2,
      language: "en",
      ner_scrubbed: false,
    });
    const result = validateJsonl(jsonl);
    expect(result.error).toMatch(/quality_signals must be an object/);
  });

  it("rejects array quality_signals", () => {
    const jsonl = line("x", [turn("user", "Hi"), turn("assistant", "Hello")], { quality_signals: [1, 2] });
    const result = validateJsonl(jsonl);
    expect(result.error).toMatch(/quality_signals must be an object/);
  });

  it("rejects non-boolean ner_scrubbed", () => {
    const jsonl = line("x", [turn("user", "Hi"), turn("assistant", "Hello")], { ner_scrubbed: "yes" });
    const result = validateJsonl(jsonl);
    expect(result.error).toMatch(/ner_scrubbed must be a boolean/);
  });

  // --- Turn validation ---

  it("rejects turn without role", () => {
    const jsonl = line("x", [{ content: "hi" }]);
    const result = validateJsonl(jsonl);
    expect(result.error).toMatch(/role and content must be strings/);
  });

  it("rejects turn without content", () => {
    const jsonl = line("x", [{ role: "user" }]);
    const result = validateJsonl(jsonl);
    expect(result.error).toMatch(/role and content must be strings/);
  });

  it("rejects non-string content (no 500, filters not skipped)", () => {
    for (const bad of [123, { x: 1 }, ["a"], true]) {
      const result = validateJsonl(line("x", [{ role: "user", content: bad }]));
      expect(result.error).toMatch(/role and content must be strings/);
    }
  });

  // --- Content filtering integration ---

  it("rejects unscrubbed email PII", () => {
    const jsonl = line("conv-1", [
      turn("user", "Email me at alice@example.com"),
      turn("assistant", "Sure"),
    ]);
    const result = validateJsonl(jsonl);
    expect(result.error).toMatch(/Unscrubbed PII.*email/);
  });

  it("rejects unscrubbed SSN", () => {
    const jsonl = line("conv-1", [
      turn("user", "My SSN is 123-45-6789"),
      turn("assistant", "Got it"),
    ]);
    const result = validateJsonl(jsonl);
    expect(result.error).toMatch(/Unscrubbed PII.*ssn/);
  });

  it("rejects blocked content", () => {
    const jsonl = line("conv-1", [
      turn("user", "how to make a bomb"),
      turn("assistant", "I cannot help with that"),
    ]);
    const result = validateJsonl(jsonl);
    expect(result.error).toMatch(/Content blocked.*dangerous_instructions/);
  });

  // --- Scrubbed placeholders should pass ---

  it("accepts scrubbed PII placeholders", () => {
    const jsonl = line("conv-1", [
      turn("user", "Email me at [EMAIL] or call [PHONE]"),
      turn("assistant", "Got your info at [PATH]"),
    ]);
    const result = validateJsonl(jsonl);
    expect(result.error).toBeUndefined();
    expect(result.count).toBe(1);
  });

  it("handles trailing newline", () => {
    const jsonl = line("conv-1", [
      turn("user", "Hello"),
      turn("assistant", "Hi"),
    ]) + "\n";
    const result = validateJsonl(jsonl);
    expect(result.count).toBe(1);
  });
});
