# Common Parlance

A privacy-preserving tool for contributing your AI conversations to an open research dataset on HuggingFace.

## What it does

Import conversations you already have, or capture new ones through a local proxy. Everything is scrubbed for PII on your machine, reviewed by you, then uploaded to the [Common Parlance dataset](https://huggingface.co/datasets/common-parlance/conversations).

```
[Your conversation exports]    [AI Client] → [Proxy :11435] → [Local Model]
            ↓                                      ↓
    common-parlance import               Automatic capture
                        ↘                ↙
                    Local SQLite database
                            ↓
                    PII scrubbing (local)
                            ↓
                    Your review & approval
                            ↓
                    Upload → Server NER
                            ↓
                    Published to dataset
```

### What scrubbed data looks like

```
Before: My friend Alice Smith at alice@gmail.com helped me set up
        the server at 192.168.1.100 in /Users/john/projects/

After:  My friend [NAME_1] at [EMAIL] helped me set up
        the server at [IP] in [PATH]
```

## Works with

**Import from**: ChatGPT exports, Claude exports, Open WebUI, Jan.ai, SillyTavern, oobabooga, OpenAI messages JSONL, ShareGPT format

**Proxy captures from**: Ollama, llama.cpp, vLLM, LM Studio, LocalAI, koboldcpp, or any OpenAI-compatible local endpoint

## Quick Start

Requires Python 3.11+.

### 1. Install

```bash
# With uv (recommended)
uv tool install common-parlance

# Or with pipx
pipx install common-parlance

# Or with pip
pip install --user common-parlance
```

This installs the `common-parlance` command on your PATH.

### 2. Register

```bash
common-parlance register
```

This opens your browser for a Cloudflare Turnstile verification (no account or email needed), then saves an anonymous API key to your local config.

### 3. Consent

```bash
common-parlance consent --grant
```

Read and agree to the contribution terms. You can revoke anytime with `common-parlance consent --revoke`.

**Note:** Revoking consent also purges all local conversation data.

### 4. Contribute conversations

**Option A — Import existing conversations:**

```bash
common-parlance import ~/Downloads/chatgpt-export.zip
common-parlance import conversations.jsonl
common-parlance import ~/jan/threads/
common-parlance import ~/.open-webui/data/webui.db
```

Format is auto-detected. Use `--dry-run` to preview without importing.

**Option B — Capture live conversations via proxy:**

```bash
common-parlance proxy

# Or run in the background
nohup common-parlance proxy > /dev/null 2>&1 &

# Or install as a service that starts on login
common-parlance startup --enable
```

Point your AI client at `http://localhost:11435` instead of the usual model URL.

**Connecting your client:**

| Client | How to connect |
|--------|---------------|
| Open WebUI | Settings → Connections → change `11434` to `11435` |
| Any OpenAI-compatible app | Set base URL to `http://localhost:11435/v1` |

**Note:** The proxy sits between your chat client and Ollama. For clients with configurable URLs (like Open WebUI), just change the port. For `ollama run`, use transparent mode — move Ollama to a different port and let the proxy take the default:

```bash
OLLAMA_HOST=127.0.0.1:11436 open -a Ollama   # macOS (or set in systemd on Linux)
common-parlance proxy --port 11434 --upstream http://localhost:11436
```

### 5. Process, review, upload

```bash
common-parlance process    # scrub PII, run audit
common-parlance review     # approve/reject/edit each conversation
common-parlance upload     # send approved conversations to the dataset
common-parlance status     # check pipeline counts
```

During review you can manually redact additional text by pressing `e` (edit) — selected text is replaced with `[REDACTED]`.

If you're using the proxy, background uploads run automatically every 24 hours for approved conversations.

## Privacy

- **Opt-in only**: nothing is captured or uploaded without your explicit consent
- **Scrubbed locally**: PII removal happens on your machine before anything leaves
- **Server-side NER**: a second pass catches names and locations that regex misses (Presidio + spaCy)
- **Anonymous**: no user ID, device fingerprint, or metadata in the published dataset
- **Inspectable**: your local data is a SQLite database you can query directly (`sqlite3 ~/.local/share/common-parlance/conversations.db`)

### What gets uploaded

- Human and assistant conversation turns only
- PII replaced with typed placeholders (`[NAME_1]`, `[EMAIL]`, `[PHONE]`, etc.)

### What gets stripped

- Model names and engine metadata
- System prompts
- Token counts, timing, performance data
- IP addresses, user agents, all client metadata

## Configuration

Settings are stored at `~/.config/common-parlance/config.json`.

```bash
common-parlance config                          # view all
common-parlance config upstream http://myhost:8080  # set a value
```

| Key | Default | Description |
|-----|---------|-------------|
| `upstream` | `http://localhost:11434` | Your local model endpoint |
| `port` | `11435` | Port the proxy listens on |
| `auto_approve` | `false` | Skip review, auto-approve all scrubbed conversations |
| `upload_interval_hours` | `24` | How often background uploads run |

## Advanced

### Local NER (optional)

Server-side NER handles name detection before publishing. For an extra local scrubbing layer:

```bash
uv pip install presidio-analyzer presidio-anonymizer spacy
python -m spacy download en_core_web_lg
```

The first time you run `process`, it will ask if you'd like to set this up.

### Watch mode

```bash
# Re-scan a directory for new exports every 60 minutes
common-parlance import ~/Downloads/ --watch 60

# Install as a system service that survives restarts
common-parlance import ~/Downloads/ --watch 60 --daemon
```

### Auto-start on login

```bash
common-parlance startup --enable   # launchd (macOS), systemd (Linux)
common-parlance startup --disable
```

## License

The **code** is licensed under Apache-2.0.

The **dataset** is licensed under [ODC-BY 1.0](LICENSE) (Open Data Commons Attribution) — use the data freely for any purpose with attribution. See [COVENANT.md](COVENANT.md) for the community request to keep model weights open.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.
