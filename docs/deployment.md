# Deployment & Dogfooding Checklist

Internal guide for standing up Common Parlance end-to-end and verifying it works.

---

## Quick Reference: Stand It Up & Test It

Sequential checklist — do these in order, check each box before moving on.

### Phase 1: Infrastructure (one-time setup)

- [ ] **Cloudflare account** — sign up (free tier)
- [ ] **HuggingFace account** — sign up, create org `common-parlance`
- [ ] **Node.js 18+** installed (for Wrangler CLI)
- [ ] **Python 3.11+** with `uv` installed
- [ ] **Local model engine** running (Ollama, llama.cpp, LM Studio, etc.) on `:11434`

### Phase 2: Deploy the NER Service (HuggingFace Spaces)

- [ ] Create a new HuggingFace Space (Docker SDK, free CPU tier)
- [ ] Upload `ner-service/Dockerfile`, `ner-service/app.py`, and `ner-service/README.md`
- [ ] Set `API_KEY` secret in Space settings (generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
- [ ] Wait for build (~2-3 minutes)
- [ ] Verify health:
  ```bash
  curl https://<your-username>-<space-name>.hf.space/health
  # Expected: {"ok":true,"model":"en_core_web_sm","entities":["PERSON","LOCATION","ORGANIZATION"]}
  ```
- [ ] Test scrubbing:
  ```bash
  curl -X POST https://<your-username>-<space-name>.hf.space/scrub \
    -H "Content-Type: application/json" \
    -H "X-API-Key: <your-ner-key>" \
    -d '{"turns": [{"role": "user", "content": "My friend Alice at Google helped me"}]}'
  # Expected: Alice → [NAME], Google → [ORG]
  ```

### Phase 3: Deploy the Worker

```bash
cd worker && npm install && npx wrangler login
```

- [ ] Create KV namespaces:
  ```bash
  npx wrangler kv namespace create "API_KEYS"
  npx wrangler kv namespace create "METRICS"
  ```
- [ ] Paste both namespace IDs into `wrangler.toml`
- [ ] Set NER service URL in `wrangler.toml`:
  ```toml
  [vars]
  NER_SERVICE_URL = "https://<your-username>-<space-name>.hf.space"
  ```
- [ ] Create a Cloudflare Turnstile widget at [dash.cloudflare.com](https://dash.cloudflare.com/?to=/:account/turnstile):
  - Widget mode: **Managed** (recommended)
  - Add hostname: `common-parlance-proxy.<your-subdomain>.workers.dev`
  - Pre-clearance: **No** (each registration should be independently verified)
- [ ] Set `TURNSTILE_SITE_KEY` in `wrangler.toml` (public, safe to commit)
- [ ] Set secrets:
  ```bash
  npx wrangler secret put HF_TOKEN       # Fine-grained, write-only to dataset repo
  npx wrangler secret put NER_API_KEY     # Same key from Phase 2
  npx wrangler secret put TURNSTILE_SECRET  # From Turnstile widget settings
  ```
- [ ] Deploy:
  ```bash
  npx wrangler deploy
  ```
- [ ] Verify health:
  ```bash
  curl https://common-parlance-proxy.<your-subdomain>.workers.dev/health
  # Expected: {"ok":true,"repo":"common-parlance/conversations"}
  ```

### Phase 4: Create the HuggingFace Dataset Repo

- [ ] Create dataset repo: `common-parlance/conversations`
- [ ] Set visibility (public or gated)
- [ ] Add dataset card (README.md with schema + ODC-BY 1.0 license reference)
- [ ] Generate a fine-grained write token scoped to this repo
- [ ] Use that token as the `HF_TOKEN` Worker secret (Phase 2)

### Phase 5: Register & Generate Admin Key

- [ ] Register via self-service (tests the full flow):
  ```bash
  common-parlance register
  ```
  This uses the device auth flow: CLI shows a code, you enter it in the browser
  with Turnstile verification + proof-of-work, and the CLI receives the key.
- [ ] Generate an admin key for `/metrics` access:
  ```bash
  python -c "import secrets; print('cp_live_' + secrets.token_hex(16))"
  npx wrangler kv key put \
    --namespace-id=<API_KEYS-namespace-id> \
    "<admin-key>" \
    '{"admin": true, "created": "2026-03-18"}'
  ```

### Phase 6: Install & Configure the Client

```bash
cd /path/to/conversation_collection
uv sync
```

- [ ] Grant consent:
  ```bash
  common-parlance consent --grant
  ```
- [ ] Register (if not done in Phase 5):
  ```bash
  common-parlance register
  ```
- [ ] Verify config:
  ```bash
  common-parlance config
  # Should show api_key configured and proxy_url set
  ```

### Phase 7: End-to-End Test

- [ ] **Start the proxy:**
  ```bash
  common-parlance proxy
  ```
  Confirm output: `Starting proxy on :11435 → http://localhost:11434`

- [ ] **Generate a test conversation:**
  ```bash
  curl http://localhost:11435/api/chat -d '{
    "model": "llama3",
    "messages": [{"role": "user", "content": "What is the capital of France?"}]
  }'
  ```
  Confirm: you get a response from your model

- [ ] **Check status (should show 1 raw):**
  ```bash
  common-parlance status
  ```

- [ ] **Process (scrub PII + content filter):**
  ```bash
  common-parlance process
  ```
  Confirm: "Processed 1 exchanges"

- [ ] **Check status (should show 1 pending review):**
  ```bash
  common-parlance status
  ```

- [ ] **Review and approve:**
  ```bash
  common-parlance review
  # Press 'a' to approve
  ```

- [ ] **Check status (should show 1 approved):**
  ```bash
  common-parlance status
  ```

- [ ] **Upload:**
  ```bash
  common-parlance upload
  ```
  Confirm: "Uploaded 1 conversations"

- [ ] **Check status (should show 1 uploaded):**
  ```bash
  common-parlance status
  ```

### Phase 8: Verify on HuggingFace

- [ ] Check the dataset repo for a new `data/batch_*.jsonl` file
- [ ] Open the file — each line should have `conversation_id` and `turns` array
- [ ] Verify turns contain only `role` and `content` (no model name, no system prompt, no metadata)
- [ ] Verify PII was scrubbed (names replaced with `[NAME_1]`, etc.)
- [ ] Spot-check: no emails, phone numbers, or other PII leaked through

### Phase 9: Verify Monitoring

- [ ] Check metrics:
  ```bash
  curl -H "X-API-Key: <admin-key>" \
    https://common-parlance-proxy.<your-subdomain>.workers.dev/metrics
  ```
  Confirm: `uploads_total` = 1, `conversations_total` = 1, `ner_entities_scrubbed` >= 0
- [ ] Check Cloudflare dashboard for Worker request logs

### Phase 10: Test Content Filter

- [ ] Send a conversation with blocked content through `process` — confirm it's skipped (never staged)
- [ ] Try uploading blocked content directly to Worker — confirm 422 response
- [ ] Check metrics: `content_blocks_total` incremented

### Phase 11: Test Failure Modes

- [ ] Upload with bad API key — confirm 401, `auth_failures_total` incremented
- [ ] Upload with invalid JSONL — confirm 422, `validation_errors_total` incremented
- [ ] Revoke consent, restart proxy — confirm conversations are NOT logged:
  ```bash
  common-parlance consent --revoke
  # restart proxy, send a conversation, check status — should be 0 raw
  ```

### Phase 12: Pre-Publish (Before Making Repo Public)

- [ ] **Rotate all secrets** — assume every key/token in `.env` or local history is compromised:
  - [ ] `HF_TOKEN`: generate new fine-grained token on HuggingFace, `npx wrangler secret put HF_TOKEN`
  - [ ] `NER_API_KEY`: `openssl rand -hex 32`, update HF Space `API_KEY` env var, then `npx wrangler secret put NER_API_KEY`
  - [ ] `TURNSTILE_SECRET`: rotate in Cloudflare dashboard, `npx wrangler secret put TURNSTILE_SECRET`
  - [ ] `CLOUDFLARE_API_TOKEN`: regenerate in Cloudflare dashboard, update GitHub repo secret
  - Rotate each secret in the relevant dashboard, then update the Worker via `npx wrangler secret put <NAME>`
- [ ] **Verify `.gitignore`** covers `.env`, `*.db`, `*.db-wal`, `*.db-shm`
- [ ] **Scan git history** for accidentally committed secrets:
  ```bash
  git log --all -p | grep -E "(hf_|cp_live_|sk-|PRIVATE KEY)" | head -20
  ```
- [ ] **Verify all secrets use separate dev/prod values** — local `.env` should not contain production tokens
- [ ] **Test all endpoints** after rotation to confirm nothing broke

---

## Remaining Work (Not Required for Testing)

### Infrastructure
- [ ] Custom domain for Worker (optional, cosmetic)
- [x] API key self-registration (device auth + Turnstile + PoW)
- [x] Dataset rollback (batch-level attribution with 90-day TTL, admin purge endpoints)
- [ ] HuggingFace fine-grained token (write-only, scoped to dataset repo)
- [ ] Tagged dataset releases for consumers to pin to known-good snapshots

### Legal / Compliance
- [ ] Complete privacy impact assessment — deferred until real usage at scale
- [~] Right-to-delete — deferred (anonymous data cannot be traced to individuals)

### Distribution
- [ ] Publish to PyPI — tag `v0.1.0`, release workflow will build + publish

### Monitoring
- [ ] HuggingFace dataset growth metrics (manual check for now)
- [ ] Alerting on high `uploads_failed` or `content_blocks_total` (Cloudflare dashboard or webhook)

### Privacy Notes

- API keys are opaque tokens — **no PII stored in KV values**
- All metrics are **aggregate counters only** — no per-user, per-conversation data
- Server logs contain **no conversation content, no API keys, no user identity**
- Content filter logs record **category only**, never the matched text
