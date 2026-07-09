/**
 * Loader + Worker-side adapter for the cp-scrub golden corpus.
 *
 * Shared spec, per-runtime adapters (see corpus/SCHEMA.md). The Worker runs in
 * workerd with NO filesystem, so the corpus is imported as a bundled module via
 * Vite's ?raw glob rather than read off disk. Same corpus/ files + same
 * validation rules as the Python loader (tests/golden_corpus.py) — that is the
 * parity safety-net.
 */

// Bundled at build time by Vite — no fs access needed inside workerd.
const RAW = import.meta.glob("../../corpus/cases/*.jsonl", {
  query: "?raw",
  import: "default",
  eager: true,
});

// Canonical entity_type -> Worker checkPii() type. The Worker has no NER, so
// name/location never carry the "worker" surface and are absent here.
const WORKER_TYPE = {
  "secret/api_key": "api_key",
  "secret/jwt": "jwt",
  "secret/private_key": "private_key",
  "secret/connection_string": "connection_string",
  "secret/bearer_token": "bearer_token",
  "secret/gcp_key_id": "gcp_key_id",
  "secret/url_signature": "url_signature",
  "secret/entropy": "high_entropy_secret",
  email: "email",
  ssn: "ssn",
  phone: "phone",
  ip: "ip",
  path: "filepath",
  credit_card: "credit_card",
};

export function workerTypeFor(entityType) {
  return WORKER_TYPE[entityType] ?? null;
}

export function gapSurfaces(span) {
  return new Set(span.known_gap ? span.known_gap.surfaces ?? [] : []);
}

function validate(c) {
  const cp = Array.from(c.input); // codepoints — the corpus offset unit
  for (const sp of c.sensitive ?? []) {
    const s = sp.start_position;
    const e = sp.end_position;
    if (!(0 <= s && s < e && e <= cp.length)) {
      throw new Error(`${c.id}: span out of range ${s}:${e} (len ${cp.length})`);
    }
    const got = cp.slice(s, e).join("");
    if (got !== sp.entity_value) {
      throw new Error(
        `${c.id}: offsets ${s}:${e} -> ${JSON.stringify(got)} != entity_value ${JSON.stringify(sp.entity_value)}`
      );
    }
    if (sp.known_gap) {
      const surfaces = new Set(c.surfaces);
      for (const g of sp.known_gap.surfaces ?? []) {
        if (!surfaces.has(g)) {
          throw new Error(`${c.id}: known_gap.surfaces ${g} not in case.surfaces`);
        }
      }
      if (!["todo", "wontfix"].includes(sp.known_gap.disposition)) {
        throw new Error(`${c.id}: bad known_gap.disposition`);
      }
    }
  }
  for (const g of c.guard ?? []) {
    if (!c.input.includes(g)) {
      throw new Error(`${c.id}: guard ${JSON.stringify(g)} not present in input`);
    }
  }
}

export function loadCorpus() {
  const cases = [];
  const seen = new Set();
  for (const path of Object.keys(RAW).sort()) {
    const text = RAW[path];
    for (const line of text.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const c = JSON.parse(trimmed);
      validate(c);
      if (seen.has(c.id)) throw new Error(`duplicate case id: ${c.id}`);
      seen.add(c.id);
      cases.push(c);
    }
  }
  return cases;
}
