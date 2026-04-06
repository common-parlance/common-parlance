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

Catches names, locations, and organizations that client-side regex scrubbing can't detect. This is a defense-in-depth layer -- the client already strips emails, phones, SSNs, IPs, file paths, and API keys before data reaches this service.

## API

### POST /scrub

```json
{
  "turns": [
    {"role": "user", "content": "My friend Alice at Google helped me debug this"},
    {"role": "assistant", "content": "That's great! Here's how to fix it..."}
  ]
}
```

Response:

```json
{
  "turns": [
    {"role": "user", "content": "My friend [NAME] at [ORG] helped me debug this"},
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
