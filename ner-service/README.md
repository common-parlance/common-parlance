---
title: Common Parlance NER
emoji: 🔒
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
---

# Common Parlance NER Service

Server-side Named Entity Recognition for the [Common Parlance](https://github.com/common-parlance/common-parlance) project.

Catches names and locations that client-side regex scrubbing can't detect. This is a defense-in-depth layer -- the client already strips emails, phones, SSNs, IPs, file paths, and API keys before data reaches this service. (Organization/product names are intentionally **not** redacted: the NER is noisy on them and they are high-utility, low-risk in technical text; genuinely sensitive internal names are caught at review.)

## API

### POST /scrub

```json
{
  "turns": [
    {"role": "user", "content": "My friend Alice in Paris helped me debug this"},
    {"role": "assistant", "content": "That's great! Here's how to fix it..."}
  ]
}
```

Response (only PERSON and LOCATION are detected — ORG is intentionally not, so
e.g. "Google" would pass through unredacted):

```json
{
  "turns": [
    {"role": "user", "content": "My friend [NAME_1] in [LOCATION] helped me debug this"},
    {"role": "assistant", "content": "That's great! Here's how to fix it..."}
  ],
  "entities_found": 2
}
```

### GET /health

Returns model info and status.

## Deployment

This is designed to run as a [HuggingFace Spaces](https://huggingface.co/docs/hub/spaces) Docker SDK app (free tier).

Set the `API_KEY` secret in your Space settings to require authentication.
