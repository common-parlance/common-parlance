/**
 * Worker runtime for the cp-scrub golden corpus (deterministic parity tier).
 *
 * Runs the trust-boundary gate checkPii() against the SAME corpus the Python
 * scrubber is pinned to. The Worker is a classifier (first-match type or null),
 * not a transformer, so the assertion is: a real secret is flagged with the
 * expected type; a clean/guard input or an all-gap input is NOT rejected (null)
 * — that null is the over-redaction guard for the gate.
 */

import { describe, it, expect } from "vitest";
import { checkPii } from "../src/content-filter.js";
import { loadCorpus, workerTypeFor, gapSurfaces } from "./corpus-loader.js";

const CASES = loadCorpus().filter((c) => c.surfaces.includes("worker"));

describe("golden corpus — worker parity (deterministic tier)", () => {
  it("loaded worker-surface cases", () => {
    expect(CASES.length).toBeGreaterThan(0);
  });

  for (const c of CASES) {
    it(c.id, () => {
      // Types the gate is expected to catch: sensitive spans that are not a
      // known gap on the worker surface and have a worker mapping.
      const expectedTypes = (c.sensitive ?? [])
        .filter((sp) => !gapSurfaces(sp).has("worker"))
        .map((sp) => workerTypeFor(sp.entity_type))
        .filter(Boolean);

      const got = checkPii(c.input);

      if (expectedTypes.length > 0) {
        expect(got).not.toBeNull();
        expect(expectedTypes).toContain(got);
      } else {
        // Pure guard, or every sensitive span is a pinned worker gap → the
        // gate must not reject (clean upload / pinned leak).
        expect(got).toBeNull();
      }
    });
  }
});
