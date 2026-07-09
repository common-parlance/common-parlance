import { describe, it, expect, afterEach, vi } from "vitest";
import {
  env,
  createExecutionContext,
  waitOnExecutionContext,
} from "cloudflare:test";
import worker, { warmNerService } from "../src/index.js";

// The miniflare test env sets NER_SERVICE_URL = "https://ner.test.invalid"
// (see vitest.config.ts). The outbound /health fetch is mocked below.

function makeController() {
  return { scheduledTime: 0, cron: "0 */8 * * *", noRetry() {} };
}

describe("scheduled — NER warm-keep", () => {
  let origFetch: typeof globalThis.fetch;

  afterEach(() => {
    if (origFetch) globalThis.fetch = origFetch;
  });

  it("pings the NER /health endpoint on schedule", async () => {
    origFetch = globalThis.fetch;
    const calls: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      calls.push(url);
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    }) as typeof globalThis.fetch;

    const ctx = createExecutionContext();
    await worker.scheduled!(makeController() as any, env, ctx);
    await waitOnExecutionContext(ctx);

    expect(calls).toEqual(["https://ner.test.invalid/health"]);
  });

  it("no-ops when NER_SERVICE_URL is unset (no outbound request)", async () => {
    origFetch = globalThis.fetch;
    const fetchMock = vi.fn();
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    await warmNerService({ NER_SERVICE_URL: "" });

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("swallows a failed ping so the cron never error-loops", async () => {
    origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async () => {
      throw new Error("network down");
    }) as typeof globalThis.fetch;

    // Must resolve, not reject, even when the NER service is unreachable.
    await expect(warmNerService(env)).resolves.toBeUndefined();
  });
});
