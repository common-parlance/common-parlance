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

## Supported Versions

Security fixes are applied to the latest release on `main`. Older versions
are not maintained.
