"""Tests for the server-side NER scrubbing service (ner-service/app.py).

The service lives outside the package (it's a standalone Docker image), so we
add its directory to sys.path. Integration tests need presidio + the spaCy
model and skip automatically when either is missing; the threshold unit test
runs only once the module imports (which loads the model), matching the
existing test_scrub_ner.py convention.
"""

import os
import pathlib
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("presidio_analyzer")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "ner-service"))

# The service now fails closed without an API key (reads API_KEY at import).
# Set one before importing the app, and authenticate every request below.
_API_KEY = "cp-ner-test-key"
os.environ["API_KEY"] = _API_KEY

try:
    import app as ner_app
except Exception as exc:  # spaCy model not downloaded, etc.
    pytest.skip(f"NER service unavailable: {exc}", allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(ner_app.app)
_HEADERS = {"X-API-Key": _API_KEY}


def _scrub(content: str) -> dict:
    resp = client.post(
        "/scrub",
        json={"turns": [{"role": "user", "content": content}]},
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_real_names_are_redacted():
    """Sanity: genuine names still get numbered, coreference-preserving tags."""
    out = _scrub("Alice Johnson met Bob Smith at the office.")
    text = out["turns"][0]["content"]
    assert "[NAME_1]" in text and "[NAME_2]" in text
    assert "Alice" not in text and "Bob" not in text


def test_programming_terms_are_not_redacted():
    """Regression: the server must not re-redact code terms the client kept.

    Before the allow_list fix, spaCy tagged these as PERSON/LOCATION at 0.85
    and the server rewrote them to [NAME_x]/[LOCATION], diverging from review.
    """
    out = _scrub("Let's use Django, deploy with Jenkins, then migrate to Go.")
    text = out["turns"][0]["content"]
    assert "Django" in text
    assert "Jenkins" in text
    assert "Go" in text
    assert out["entities_found"] == 0


def test_scrub_rejects_missing_api_key():
    """Fail closed: no key -> 401, and nothing is scrubbed."""
    resp = client.post(
        "/scrub", json={"turns": [{"role": "user", "content": "Alice Johnson"}]}
    )
    assert resp.status_code == 401


def test_scrub_rejects_wrong_api_key():
    resp = client.post(
        "/scrub",
        json={"turns": [{"role": "user", "content": "Alice Johnson"}]},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


def test_scrub_rejects_oversized_body():
    """Body-size cap: a body over MAX_REQUEST_BYTES is rejected with 413,
    independent of the Content-Length header (the stream is bounded as read)."""
    big = "a" * (ner_app.MAX_REQUEST_BYTES + 1024)
    resp = client.post(
        "/scrub",
        json={"turns": [{"role": "user", "content": big}]},
        headers=_HEADERS,
    )
    assert resp.status_code == 413


def test_filter_by_threshold_drops_below_cutoff():
    """Deterministic unit test of the per-entity threshold filter."""
    results = [
        SimpleNamespace(entity_type="PERSON", score=0.85),
        SimpleNamespace(entity_type="PERSON", score=0.84),
        SimpleNamespace(entity_type="LOCATION", score=0.70),
        SimpleNamespace(entity_type="LOCATION", score=0.65),
        SimpleNamespace(entity_type="IBAN_CODE", score=0.50),  # default cutoff
        SimpleNamespace(entity_type="IBAN_CODE", score=0.49),
    ]
    kept = {(r.entity_type, r.score) for r in ner_app._filter_by_threshold(results)}
    assert ("PERSON", 0.85) in kept and ("PERSON", 0.84) not in kept
    assert ("LOCATION", 0.70) in kept and ("LOCATION", 0.65) not in kept
    assert ("IBAN_CODE", 0.50) in kept and ("IBAN_CODE", 0.49) not in kept
