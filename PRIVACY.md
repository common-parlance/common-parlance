# Privacy Policy

**Common Parlance**
Last updated: 2026-03-12

## What Common Parlance Is

Common Parlance is an open source tool that lets you contribute your local AI
conversations to a community-owned open dataset. It sits between your AI
client and your local model engine, capturing conversations you choose to share.

## What We Collect

### Conversation turns

When you opt in, Common Parlance logs the text of your conversations with local
AI models — specifically the human (user) and assistant message turns.

Before anything leaves your device, all conversations go through PII
(personally identifiable information) scrubbing that replaces names, emails,
phone numbers, addresses, and other identifiers with typed placeholders like
`[NAME_1]`, `[EMAIL]`, `[PHONE]`.

### What we do NOT collect

- Your name, email, or any account information
- IP addresses, device identifiers, or browser/client metadata
- Model names, system prompts, or engine configuration
- Token counts, timing data, or performance metrics
- Location data or any form of geolocation
- Usage analytics or telemetry

## How Your Data Is Processed

Processing happens in two stages — local and server-side:

**On your machine (before data leaves your device):**

1. **Capture**: Conversations are stored in a local SQLite database that you own
   and can inspect at any time. Raw conversations (before PII scrubbing) remain
   in the database until you run the `process` command. If you don't process
   regularly, raw data may persist indefinitely on your machine.
2. **Local PII scrubbing**: Emails, phone numbers, credit card numbers, SSNs,
   IP addresses, file paths, API keys, and secrets are detected and replaced
   with placeholders using regex pattern matching. Optionally, you can install
   Presidio/spaCy for local name detection as well.
3. **Content filtering**: Conversations are checked against a blocklist for
   harmful content (CSAM indicators, dangerous instructions). Blocked content
   is never uploaded.
4. **Review**: You can review and approve or reject each conversation before
   upload. Or you can enable auto-approve if you prefer not to review.

**On our servers (before data is published):**

5. **Server-side PII validation**: Our upload proxy rejects any data that still
   contains detectable structured PII (emails, phones, SSNs, file paths, API
   keys). This catches cases where client scrubbing was bypassed or incomplete.
6. **Server-side NER**: A Named Entity Recognition service (Presidio + spaCy)
   scans for names, locations, and organizations that regex cannot detect. This
   is the primary defense for unstructured PII like names mentioned in text.
   **Note:** The NER model is English-only. Names and locations in other
   languages may not be detected by this pass. Non-English conversations
   rely primarily on the regex scrubbing stage for PII protection.
7. **Upload**: Data that passes all checks is committed to the community dataset
   on HuggingFace.

We never see your raw conversations. Structured PII (emails, phones, etc.) is
stripped locally before leaving your device. Scrubbed conversation text
(with structured PII already replaced by placeholders) is sent over HTTPS to
our NER service for name/location detection, then scrubbed again before
publishing.

## Legal Basis for Processing

We process your data based on your **explicit consent**. You must opt in
before any data is collected. The proxy works normally without consent — it
just doesn't log or upload conversations.

## Anonymization

Once PII is scrubbed and metadata is stripped, the uploaded data is anonymous.
It contains no user identifiers, device fingerprints, timestamps, or any
information that could be used to identify who contributed a particular
conversation.

Because anonymous data cannot be linked to any individual, data protection
regulations generally do not apply to the published dataset.

## Data Storage

- **Local data** (raw conversations, staged conversations): Stored in a SQLite
  database on your machine. You have full control — you can inspect, export, or
  delete this file at any time.
- **Uploaded data** (anonymous, scrubbed conversations): Stored in a public
  dataset on HuggingFace under the ODC-BY 1.0 license.
- **Deduplication hashes**: SHA-256 hashes of scrubbed conversation content are
  stored on our upload proxy for 30 days to prevent duplicate uploads. These are
  one-way hashes of already-anonymized text and cannot be used to recover
  conversation content.

## Data Retention

- **Local data**: Retained until you delete it. We do not automatically purge
  local data.
- **Contribution tracking**: For 90 days after upload, we retain a mapping
  between your API key prefix and the batch files you uploaded. This allows us
  to honor deletion requests — if you ask, we can identify and remove your
  contributions within that window.
- **After 90 days**: The contribution mapping automatically expires. Your
  uploaded data remains in the dataset but is permanently anonymous — it can
  no longer be traced to any API key or individual, and cannot be selectively
  removed.

## Your Rights

### Before upload (local data)

You have full control over your local data:

- **Access**: Your SQLite database is on your machine. Inspect it anytime.
- **Rectification**: Review and reject conversations before upload.
- **Erasure**: Delete the SQLite database file to remove all local data.
- **Portability**: The SQLite file is your data — copy or export it freely.
- **Object / Withdraw consent**: Run `common-parlance consent --revoke` to stop
  all future collection. The proxy continues to work without logging.

### After upload

- **Within 90 days**: We can identify your contributions via the upload
  tracking described above. Contact us to request removal.
- **After 90 days**: The tracking mapping expires automatically. Contributions
  become permanently anonymous and cannot be identified or removed.

Revoking consent stops all future uploads immediately.

## Third Parties

- **HuggingFace**: Hosts the public dataset and our NER scrubbing service
  (HuggingFace Spaces). Subject to HuggingFace's own
  [privacy policy](https://huggingface.co/privacy).
- **Cloudflare**: Our upload proxy runs on Cloudflare Workers. Cloudflare may
  process request metadata (IP addresses, headers) per their
  [privacy policy](https://www.cloudflare.com/privacypolicy/). We do not store
  or log this information.

We do not sell, share, or provide your data to any other third parties.

## Children

Common Parlance is not directed at children under 16. We do not knowingly
collect data from children.

## Changes to This Policy

We may update this policy as the project evolves. Changes will be reflected in
the "Last updated" date above and committed to the project repository.

## Contact

For privacy questions or concerns, open an issue at:
https://github.com/common-parlance/common-parlance/issues

## Supervisory Authority

If you believe your data protection rights have been violated, you have the
right to lodge a complaint with your local data protection supervisory
authority.
