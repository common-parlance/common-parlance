# Contributing to Common Parlance

Thanks for your interest in contributing! This project has two parts: a
Python package (the proxy + CLI) and a Cloudflare Worker (the upload proxy).

## Dev Setup

### Python (proxy + CLI)

```bash
# Clone and install
git clone https://github.com/common-parlance/common-parlance.git
cd common-parlance
uv sync --group dev

# Optional: local NER (name detection)
uv pip install common-parlance[ner]
uv run python -m spacy download en_core_web_lg
```

### Worker (upload proxy)

```bash
cd worker
npm install
npx wrangler dev  # local dev server
```

Note: some Worker functionality requires secrets (API keys, HuggingFace token).
Local dev works for route handling and validation logic.

## Running Tests

```bash
# Python tests
uv run pytest tests/ -q

# Worker tests
cd worker && npm test

# Lint + format (must pass before merge)
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

CI runs these same commands on every push and PR.

### NER service

The `ner-service/` directory contains a Presidio + spaCy NER service deployed
on HuggingFace Spaces. If you're modifying PII scrubbing logic, test against
the NER service locally:

```bash
cd ner-service
docker build -t ner-service .
docker run -p 7860:7860 ner-service
# Or without Docker:
pip install fastapi uvicorn presidio-analyzer presidio-anonymizer spacy
python -m spacy download en_core_web_sm
uvicorn app:app --port 7860
```

## Submitting Changes

1. Fork the repo and create a branch from `main`
2. Make your changes — keep PRs focused on one thing
3. Add tests for new functionality
4. Make sure `uv run pytest` and `uv run ruff check` pass
5. Open a pull request

Ruff handles formatting and style automatically. Run `uv run ruff format src/ tests/`
to auto-fix formatting issues.

## Reporting Bugs

Open an issue using the [bug report template](https://github.com/common-parlance/common-parlance/issues/new?template=bug_report.yml).

To report content concerns in the dataset, use the
[content report template](https://github.com/common-parlance/common-parlance/issues/new?template=content_report.yml).

## License

Code contributions are licensed under Apache-2.0. By submitting a PR, you agree
to license your contribution under the same terms.

The dataset license (ODC-BY 1.0) applies to contributed conversation data, not
to code contributions.
