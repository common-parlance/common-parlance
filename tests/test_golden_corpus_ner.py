"""NER tier for the cp-scrub golden corpus (probabilistic, non-blocking).

Separate from the deterministic tier: names/locations come from Presidio + spaCy,
whose output shifts with model version, so this suite is Python-only (workerd has
no NER) and is scored against the models pinned in manifest.ner_models. It is
non-blocking by construction — if the spaCy models aren't installed (e.g. CI,
which doesn't download them) the whole module skips.

Two surfaces are pinned from the same cases:
  * scrub — the client PresidioScrubber (en_core_web_lg)
  * ner   — the server ner-service FastAPI app (en_core_web_sm), driven via its
            real /scrub endpoint through a TestClient.

A model-recall divergence between lg and sm is recorded as a per-surface
known_gap (the parity pin), exactly like the deterministic tier's leaks.
"""

import importlib.util
import os
import re
from pathlib import Path

import pytest

from tests.golden_corpus import gap_surfaces, load_corpus

pytest.importorskip("presidio_analyzer")

_REQUIRED_MODELS = ("en_core_web_lg", "en_core_web_sm")
_MISSING = [m for m in _REQUIRED_MODELS if importlib.util.find_spec(m) is None]
if _MISSING:
    pytest.skip(f"spaCy models not installed: {_MISSING}", allow_module_level=True)

from common_parlance.scrub import PresidioScrubber  # noqa: E402

_NER_CASES = load_corpus(tier="ner")
_NAME_TOKEN_RE = re.compile(r"\[NAME_\d+\]")

# Client surface: PresidioScrubber on en_core_web_lg.
_client = PresidioScrubber()


def _make_server_scrub():
    """Load the in-repo ner-service app (en_core_web_sm) and return a function
    that scrubs one text through its real /scrub endpoint."""
    os.environ["SPACY_MODEL"] = "en_core_web_sm"
    os.environ["API_KEY"] = "cp-ner-test-key"  # service now fails closed
    app_path = Path(__file__).resolve().parent.parent / "ner-service" / "app.py"
    spec = importlib.util.spec_from_file_location("cp_ner_app", app_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from fastapi.testclient import TestClient

    client = TestClient(mod.app)
    headers = {"X-API-Key": "cp-ner-test-key"}

    def scrub(text: str) -> str:
        resp = client.post(
            "/scrub",
            json={"turns": [{"role": "user", "content": text}]},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["turns"][0]["content"]

    return scrub


_SURFACE_SCRUB = {"scrub": _client.scrub, "ner": _make_server_scrub()}


def _names(case):
    return [s for s in case.sensitive if s["entity_type"] == "name"]


def _locations(case):
    return [s for s in case.sensitive if s["entity_type"] == "location"]


def test_corpus_has_ner_cases():
    assert _NER_CASES, "no ner-tier cases loaded from corpus"


@pytest.mark.parametrize("surface", ["scrub", "ner"])
@pytest.mark.parametrize("case", _NER_CASES, ids=lambda c: c.id)
def test_ner_parity(case, surface):
    if surface not in case.surfaces:
        pytest.skip(f"{case.id} does not pin surface {surface!r}")
    out = _SURFACE_SCRUB[surface](case.input)

    # Known gaps: this surface's model misses the entity — pin the leak so the
    # suite stays green on documented behavior and goes RED (the good kind) if a
    # model upgrade closes it.
    for span in case.sensitive:
        if surface in gap_surfaces(span):
            val = span["entity_value"]
            assert val in out, (
                f"{case.id}/{surface}: known_gap expected a leak, but {val!r} "
                f"was redacted — model improved? review the corpus flag"
            )

    # Names: redaction + coreference numbering, derived from the spans. Distinct
    # casefolded forms (reading order) map to [NAME_1], [NAME_2], ...
    nongap_names = [s for s in _names(case) if surface not in gap_surfaces(s)]
    numbering: dict[str, str] = {}
    for s in sorted(nongap_names, key=lambda s: s["start_position"]):
        key = " ".join(s["entity_value"].split()).casefold()
        numbering.setdefault(key, f"[NAME_{len(numbering) + 1}]")
        assert s["entity_value"] not in out, (
            f"{case.id}/{surface}: leaked name {s['entity_value']!r} -> {out!r}"
        )
    for ph in set(numbering.values()):
        assert ph in out, f"{case.id}/{surface}: expected {ph} in {out!r}"
    distinct = set(_NAME_TOKEN_RE.findall(out))
    assert len(distinct) == len(numbering), (
        f"{case.id}/{surface}: expected {len(numbering)} distinct [NAME_n] "
        f"(coreference), got {sorted(distinct)} in {out!r}"
    )

    # Locations: redaction + flat [LOCATION] (not numbered).
    nongap_locs = [s for s in _locations(case) if surface not in gap_surfaces(s)]
    for s in nongap_locs:
        assert s["entity_value"] not in out, (
            f"{case.id}/{surface}: leaked location {s['entity_value']!r} -> {out!r}"
        )
    if nongap_locs:
        assert "[LOCATION]" in out, (
            f"{case.id}/{surface}: expected [LOCATION] in {out!r}"
        )

    # Guards: over-redaction net — must survive.
    for g in case.guard:
        assert g in out, f"{case.id}/{surface}: over-redacted guard {g!r} -> {out!r}"
