# Changelog

All notable changes to Common Parlance are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); from 0.1.0 onward the
project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0/).

The version is defined once, in `src/common_parlance/__init__.py`.

## [Unreleased]

Pre-launch hardening toward the first public release. Highlights:

### Security & privacy
- Closed PII/secret leaks in the scrubber: PEM bodies, compressed IPv6,
  database connection strings, all-caps secret smuggling, and IPv4
  octet/sentence-period handling; replaced a leaky word-likeness heuristic with
  a 5731-word dictionary rejector that is byte-identical across the Python and
  Worker runtimes.
- Bounded the email / connection-string / credit-card regexes against ReDoS.
- Hardened the Cloudflare Worker trust boundary: `[URL:]` placeholder smuggling,
  placeholder-glue bypass, non-string content, homoglyph/leet folding, and PII
  in the previously unscanned `language`/`role`/`quality_signals` fields.
- Content filter: catch `a`/`an` and separator/concatenation CSAM evasions
  (`child.porn` / `childporn`) in both runtimes.
- Wired the optional Detoxify ML toxicity filter as a real second layer behind
  the keyword blocklist (on by default when the `[ml]` extra is installed;
  `use_ml_filter` config opt-out). It was previously built but never invoked.
- Fail-safe defaults: auth fails closed on non-JSON metadata; upload fails
  closed when NER is unconfigured (`ALLOW_NO_NER` opt-out); atomic native rate
  limiters bound bursts; the importer is hardened against zip-bombs and
  malformed conversations; the ner-service fails closed without `API_KEY` and
  bounds the request body as it reads.

### Changed (honesty / docs)
- Reframed all user-facing copy from "anonymous" to risk-reduced /
  metadata-stripped (consent prompt, TERMS, PRIVACY, dataset card) — token-level
  scrubbing cannot deliver anonymity.
- Disclosed the English-only PII scope at the consent moment; `process` now
  warns when non-English conversations are staged (both NER passes are
  English-only).
- PRIVACY states the exact IP handling — a short salted one-way rate-limit hash
  and a Turnstile forward, with no raw IP stored and no request logs.
- Tagged the dataset `en`, not `multilingual`.
- License split: code under Apache-2.0 (`LICENSE`), dataset under ODC-BY
  (`LICENSE-DATASET`); `NOTICE` attributes the vendored wordlist.

### Ops / packaging
- sdist scoped via an allowlist so the Worker's `wrangler.toml` (live KV ids)
  and dev cruft never ship to PyPI.
- CI gates the NER/PII test tier (was silently skipping) and now lints
  `ner-service/`.
- Version single-sourced from `__init__.py`; dependency upper bounds added;
  ner-service base image digest-pinned; pre-commit config with a secret scanner.
- Registration: dropped the standalone proof-of-work (Turnstile is the gate) in
  favor of a native rate-limit binding.

## [0.1.0] — unreleased

Initial public release (in preparation).
