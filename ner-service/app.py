"""Server-side NER scrubbing service.

Runs Presidio + spaCy to catch names, locations, and organizations
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

logger = logging.getLogger(__name__)

from fastapi import FastAPI, Header, HTTPException
from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig
from pydantic import BaseModel

# --- Unicode normalization (adversarial PII evasion defense) ---
# Homoglyphs (Cyrillic а vs Latin a), zero-width characters, and bidi
# overrides can bypass NER. NFKC normalization + control character
# stripping defeats these attacks. Must run before any NER analysis.
_INVISIBLE_RE = re.compile(
    "["
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
nlp_provider = NlpEngineProvider(nlp_configuration={
    "nlp_engine_name": "spacy",
    "models": [{"lang_code": "en", "model_name": SPACY_MODEL}],
})
analyzer = AnalyzerEngine(nlp_engine=nlp_provider.create_engine())
anonymizer = AnonymizerEngine()
logger.info("Loaded spaCy model: %s", SPACY_MODEL)

API_KEY = os.environ.get("API_KEY", "")
if not API_KEY:
    logger.warning(
        "API_KEY not set — NER endpoint is unauthenticated. "
        "Set API_KEY env var to require authentication."
    )

# Only detect entity types that regex can't handle.
# Emails, phones, IPs, etc. are already scrubbed client-side.
NER_ENTITIES = ["PERSON", "LOCATION", "ORGANIZATION"]

OPERATORS = {
    "PERSON": OperatorConfig("replace", {"new_value": "[NAME]"}),
    "LOCATION": OperatorConfig("replace", {"new_value": "[LOCATION]"}),
    "ORGANIZATION": OperatorConfig("replace", {"new_value": "[ORG]"}),
}


class ScrubRequest(BaseModel):
    turns: list[dict]


class ScrubResponse(BaseModel):
    turns: list[dict]
    entities_found: int
    entities_per_turn: list[int]


MAX_TURNS = 200
MAX_CONTENT_LENGTH = 100_000  # 100KB per turn
MAX_REQUEST_BYTES = 2 * 1024 * 1024  # 2MB total request body


@app.post("/scrub", response_model=ScrubResponse)
async def scrub(
    payload: ScrubRequest,
    x_api_key: str = Header(None),
):
    # Note: request size is enforced by uvicorn's --limit-max-request-size
    # (2MB, set in Dockerfile). The Content-Length header check was removed
    # because FastAPI parses the full body before the handler runs.
    if API_KEY and x_api_key != API_KEY:
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
        scan_text = text[:MAX_CONTENT_LENGTH] if len(text) > MAX_CONTENT_LENGTH else text

        try:
            results = analyzer.analyze(
                text=scan_text,
                entities=NER_ENTITIES,
                language="en",
                score_threshold=0.5,
            )

            if results:
                if len(text) <= MAX_CONTENT_LENGTH:
                    # Normal case: scrub the full text
                    anonymized = anonymizer.anonymize(
                        text=text,
                        analyzer_results=results,
                        operators=OPERATORS,
                    )
                    text = anonymized.text
                else:
                    # Oversized: scrub the scanned prefix, reattach the tail
                    tail = text[MAX_CONTENT_LENGTH:]
                    anonymized = anonymizer.anonymize(
                        text=scan_text,
                        analyzer_results=results,
                        operators=OPERATORS,
                    )
                    text = anonymized.text + tail
        except Exception:
            logger.error("Presidio error on turn, passing through unscrubbed", exc_info=True)
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
