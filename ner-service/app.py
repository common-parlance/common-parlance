"""Server-side NER scrubbing service.

Runs Presidio + spaCy to catch names and locations
that client-side regex scrubbing can't detect. Deployed on HuggingFace
Spaces (free tier) as a Docker SDK Space.

The client already handles structured PII (emails, phones, SSNs, IPs,
file paths, API keys). This service is a defense-in-depth layer that
catches unstructured PII (names mentioned in conversation text).
"""

import logging
import os
import re
import unicodedata

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# --- Unicode normalization (adversarial PII evasion defense) ---
# Homoglyphs (Cyrillic а vs Latin a), zero-width characters, and bidi
# overrides can bypass NER. NFKC normalization + control character
# stripping defeats these attacks. Must run before any NER analysis.
_INVISIBLE_RE = re.compile(
    "["
    "\u061c"  # arabic letter mark (bidi control)
    "\u200b"  # zero-width space
    "\u200c"  # zero-width non-joiner
    "\u200d"  # zero-width joiner
    "\u200e"  # left-to-right mark
    "\u200f"  # right-to-left mark
    "\u202a"  # left-to-right embedding
    "\u202b"  # right-to-left embedding
    "\u202c"  # pop directional formatting
    "\u202d"  # left-to-right override
    "\u202e"  # right-to-left override
    "\u2060"  # word joiner
    "\u2061"  # function application
    "\u2062"  # invisible times
    "\u2063"  # invisible separator
    "\u2064"  # invisible plus
    "\u2066"  # left-to-right isolate
    "\u2067"  # right-to-left isolate
    "\u2068"  # first strong isolate
    "\u2069"  # pop directional isolate
    "\ufeff"  # byte order mark / zero-width no-break space
    "\ufff9"  # interlinear annotation anchor
    "\ufffa"  # interlinear annotation separator
    "\ufffb"  # interlinear annotation terminator
    "]+",
)


def _normalize_text(text: str) -> str:
    """Normalize text to defeat adversarial PII evasion.

    NFKC normalization maps homoglyphs to canonical Latin forms.
    Invisible character stripping removes zero-width spaces, joiners,
    and bidi overrides that break NER tokenization.
    """
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE_RE.sub("", text)
    return text


app = FastAPI(title="Common Parlance NER Service", docs_url=None, redoc_url=None)

# Initialize once at startup (not per-request).
# SPACY_MODEL env var allows using en_core_web_lg locally for better
# accuracy while keeping en_core_web_sm on HF Spaces (free tier RAM).
SPACY_MODEL = os.environ.get("SPACY_MODEL", "en_core_web_sm")
nlp_provider = NlpEngineProvider(
    nlp_configuration={
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": SPACY_MODEL}],
    }
)
analyzer = AnalyzerEngine(nlp_engine=nlp_provider.create_engine())
anonymizer = AnonymizerEngine()
logger.info("Loaded spaCy model: %s", SPACY_MODEL)

API_KEY = os.environ.get("API_KEY", "")
if not API_KEY:
    logger.warning(
        "API_KEY not set — /scrub will REJECT all requests (fail closed). "
        "Set API_KEY to enable the endpoint."
    )

# Hard cap on the request body. Enforced here (a middleware that checks
# Content-Length up front) rather than via a uvicorn flag — uvicorn has no such
# option, so the previously-documented limit did not exist.
MAX_REQUEST_BYTES = 2 * 1024 * 1024  # 2MB


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    # Fast path: reject a declared Content-Length over the cap up front.
    cl = request.headers.get("content-length")
    if cl is not None and cl.isdigit() and int(cl) > MAX_REQUEST_BYTES:
        return JSONResponse(status_code=413, content={"detail": "Request too large"})
    # Robust path: a chunked or absent/non-numeric Content-Length would bypass
    # the header check, so also bound the body as we read it. Buffer up to the
    # cap and cache it on the request so the route can still parse it (the
    # stream is consumed here); reject the moment the cap is exceeded.
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_REQUEST_BYTES:
            return JSONResponse(
                status_code=413, content={"detail": "Request too large"}
            )
    request._body = body
    return await call_next(request)


# Only detect entity types that regex can't handle.
# Emails, phones, IPs, etc. are already scrubbed client-side.
# ORGANIZATION is intentionally excluded: spaCy ORG NER is noisy
# (misclassifies products/tools), org names are high-utility and low-risk in
# coding data, and sensitive internal names are caught at review. Keep in
# sync with scrub.py's analyzed entity set.
NER_ENTITIES = ["PERSON", "LOCATION"]

# Programming terms spaCy NER misclassifies as PERSON/LOCATION ("Django", "Go",
# "Jenkins"). Presidio's spaCy recognizer stamps every NER hit with a fixed
# score (0.85), so score thresholds can't separate a library name from a real
# person — only an allow_list can. Without this the server re-redacts code terms
# the client deliberately kept (scrub.py passes the same allow_list), so a
# reviewed "Django" silently becomes "[NAME_1]" in the published trace.
# DUPLICATED from scrub.py:_PROGRAMMING_ALLOW_LIST as a stopgap until the shared
# cp-scrub engine (Go-To-Market roadmap Phase 1). Keep the two lists in sync.
_PROGRAMMING_ALLOW_LIST = [
    # languages
    "Python",
    "Java",
    "Ruby",
    "Rust",
    "Swift",
    "Kotlin",
    "Scala",
    "Julia",
    "Perl",
    "Lua",
    "Dart",
    "Elixir",
    "Fortran",
    "Pascal",
    "Haskell",
    "Erlang",
    "Clojure",
    "Groovy",
    "C",
    "R",
    "Go",
    # tools / platforms
    "Git",
    "Docker",
    "Kubernetes",
    "Terraform",
    "Ansible",
    "Jenkins",
    "Gradle",
    "Maven",
    "Cargo",
    "Helm",
    "Vagrant",
    "Nginx",
    "Apache",
    "Redis",
    "Kafka",
    "Celery",
    "Pandas",
    "NumPy",
    "Flask",
    "Django",
    "FastAPI",
    "Rails",
    "Spring",
    "Node",
    "Deno",
    "Bun",
    # CS terms
    "Boolean",
    "Lambda",
    "Mutex",
    "Regex",
    # AI/ML
    "Transformer",
    "BERT",
    "GPT",
    "LLM",
    "CUDA",
    "PyTorch",
    "TensorFlow",
    "Keras",
    "Llama",
    "Claude",
    "Gemini",
    # frameworks / libraries
    "React",
    "Angular",
    "Vue",
    "Svelte",
    "jQuery",
    "Bootstrap",
    "Tailwind",
    "Express",
    "Nest",
    "Next",
    "Nuxt",
    "Remix",
    # math / algorithms
    "Fibonacci",
    "Dijkstra",
    "Euler",
]
# Presidio's allow_list is case-sensitive, so include original + lowercase forms.
_ALLOW_LIST = sorted(
    {t for term in _PROGRAMMING_ALLOW_LIST for t in (term, term.lower())}
)

# Per-entity score thresholds, mirroring scrub.py's _filter_results. spaCy NER
# is fixed at 0.85, so these are mostly a guard for pattern/context-scored
# entities; kept for parity with the client's documented thresholds.
_ENTITY_SCORE_THRESHOLDS = {"PERSON": 0.85, "LOCATION": 0.70}
_DEFAULT_SCORE_THRESHOLD = 0.5


def build_operators(results: list, text: str) -> dict:
    """Operators for a single turn. PERSON entities get consistent numbered
    [NAME_1]/[NAME_2] placeholders (reading order) to preserve coreference,
    matching scrub.py and the documented design; LOCATION/ORG stay flat.
    """
    mapping: dict[str, str] = {}
    persons = sorted(
        (r for r in results if r.entity_type == "PERSON"),
        key=lambda r: r.start,
    )
    for r in persons:
        key = " ".join(text[r.start : r.end].split()).casefold()
        if key not in mapping:
            mapping[key] = f"[NAME_{len(mapping) + 1}]"

    def _replace(value: str) -> str:
        key = " ".join(value.split()).casefold()
        placeholder = mapping.get(key)
        if placeholder is None:
            placeholder = f"[NAME_{len(mapping) + 1}]"
            mapping[key] = placeholder
        return placeholder

    return {
        "PERSON": OperatorConfig("custom", {"lambda": _replace}),
        "LOCATION": OperatorConfig("replace", {"new_value": "[LOCATION]"}),
    }


def _filter_by_threshold(results: list) -> list:
    """Drop entities scoring below their per-entity threshold.

    Parity with scrub.py._filter_results — over-redaction guard for any
    pattern/context-scored entity that comes back below the cutoff.
    """
    return [
        r
        for r in results
        if r.score
        >= _ENTITY_SCORE_THRESHOLDS.get(r.entity_type, _DEFAULT_SCORE_THRESHOLD)
    ]


class ScrubRequest(BaseModel):
    turns: list[dict]


class ScrubResponse(BaseModel):
    turns: list[dict]
    entities_found: int
    entities_per_turn: list[int]


MAX_TURNS = 200
MAX_CONTENT_LENGTH = 100_000  # 100KB per turn


@app.post("/scrub", response_model=ScrubResponse)
async def scrub(
    payload: ScrubRequest,
    x_api_key: str = Header(None),
):
    # Fail closed: with no API_KEY configured, reject everything (don't run as
    # an open public Presidio endpoint). Body size is bounded by the
    # limit_body_size middleware.
    if not API_KEY or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Best-effort: process up to MAX_TURNS, skip the rest
    turns_to_process = payload.turns[:MAX_TURNS]

    scrubbed_turns = []
    per_turn_counts = []

    for turn in turns_to_process:
        text = turn.get("content", "")
        role = turn.get("role", "")

        # Unicode normalization before NER (defeats homoglyph/zero-width evasion)
        text = _normalize_text(text)

        # For oversized turns, run NER on the first chunk only.
        # The full content is still passed through — we just scan
        # what we can. A missed entity past the limit is acceptable
        # since client-side regex already handled structured PII.
        scan_text = (
            text[:MAX_CONTENT_LENGTH] if len(text) > MAX_CONTENT_LENGTH else text
        )

        try:
            results = analyzer.analyze(
                text=scan_text,
                entities=NER_ENTITIES,
                language="en",
                score_threshold=_DEFAULT_SCORE_THRESHOLD,
                allow_list=_ALLOW_LIST,
            )
            # Allow-list + per-entity thresholds for parity with scrub.py, so the
            # server doesn't re-redact code terms the client deliberately kept.
            results = _filter_by_threshold(results)

            if results:
                if len(text) <= MAX_CONTENT_LENGTH:
                    # Normal case: scrub the full text
                    anonymized = anonymizer.anonymize(
                        text=text,
                        analyzer_results=results,
                        operators=build_operators(results, text),
                    )
                    text = anonymized.text
                else:
                    # Oversized: scrub the scanned prefix, reattach the tail
                    tail = text[MAX_CONTENT_LENGTH:]
                    anonymized = anonymizer.anonymize(
                        text=scan_text,
                        analyzer_results=results,
                        operators=build_operators(results, scan_text),
                    )
                    text = anonymized.text + tail
        except Exception:
            logger.error(
                "Presidio error on turn, passing through unscrubbed", exc_info=True
            )
            results = []

        per_turn_counts.append(len(results) if results else 0)
        scrubbed_turns.append({"role": role, "content": text})

    # Truncate turns beyond MAX_TURNS rather than passing them unscrubbed
    if len(payload.turns) > MAX_TURNS:
        logger.warning(
            "Truncated %d turns beyond MAX_TURNS (%d)",
            len(payload.turns) - MAX_TURNS,
            MAX_TURNS,
        )

    return ScrubResponse(
        turns=scrubbed_turns,
        entities_found=sum(per_turn_counts),
        entities_per_turn=per_turn_counts,
    )


@app.get("/health")
async def health():
    return {"ok": True, "model": SPACY_MODEL, "entities": NER_ENTITIES}
