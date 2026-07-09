# Security Policy

## Reporting a Vulnerability

If you believe you've found a security vulnerability in Common Parlance,
please report it privately rather than opening a public issue.

**How to report:**

- Open a [private security advisory](https://github.com/common-parlance/common-parlance/security/advisories/new) on GitHub, or
- Email the maintainers (address in the GitHub profile of the repository owner).

Please include:

- A description of the issue and its potential impact
- Steps to reproduce
- Any proof-of-concept code or configuration

We will acknowledge your report, investigate, and keep you informed of progress
toward a fix. We aim to respond within a few days.

## Scope

In-scope:

- The Python client (proxy, CLI, PII scrubbing)
- The Cloudflare Worker (auth, upload proxy, content filter)
- The NER service (FastAPI + Presidio)
- Anything that could cause PII to leak into the published dataset or bypass
  the consent/review pipeline

Out of scope:

- Denial-of-service against the rate-limited Cloudflare Worker
- Findings that require a malicious local AI model

## Local data at rest

The proxy logs raw conversations to a local SQLite database (default under your
user data directory) so they can be scrubbed, reviewed, and uploaded. That file
holds **un-scrubbed** conversation text until you process and purge it, so its
protection matters.

What the tool does:

- **Owner-only permissions** (`0600`) on the database and its WAL/SHM
  sidecars, set explicitly on open.
- **`PRAGMA secure_delete=ON`** so freed pages are zeroed rather than left
  on disk, and purge runs `checkpoint → VACUUM → checkpoint` so deleted text
  is not retained in the WAL.
- **Aggressive purge:** `purge_processed_raw` drops raw exchanges once
  processed; `purge_all` clears everything. Keep the staging window short.

What the tool deliberately does **not** do, and why:

- **No application-level database encryption (e.g. SQLCipher).** A local CLI
  has no user secret to derive a key from: storing the key next to the
  database (or in the OS keychain the same process can read unattended) gives
  an attacker with read access to your account both halves, so it would add
  operational complexity and a false sense of security without changing the
  threat model. At-rest confidentiality is delegated to **full-disk
  encryption** (FileVault / LUKS / BitLocker), which protects the realistic
  threat (a lost or stolen device) without a key-management catch-22.

**Recommendation:** enable full-disk encryption on any machine running the
proxy, and purge processed data promptly. The threat model assumes your user
account is not already compromised — an attacker running as your user can read
the database regardless of any at-rest scheme a same-user process could apply.

## Supported Versions

Security fixes are applied to the latest release on `main`. Older versions
are not maintained.
