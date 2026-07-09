"""Loader + Python-side adapters for the cp-scrub golden corpus.

Shared spec, per-runtime adapters (see corpus/SCHEMA.md). This is the Python
runtime; the Worker has its own loader at worker/test/corpus-loader.js. Both
read the SAME corpus/ files and enforce the SAME validation rules — that is the
parity safety-net.
"""

from __future__ import annotations

import json
from pathlib import Path

CORPUS_DIR = Path(__file__).resolve().parent.parent / "corpus"

# Canonical entity_type -> scrub.py placeholder. The whole secret/* subtree
# collapses to [SECRET]; structured PII maps 1:1. URL/NAME are dynamic, so we
# match on a prefix ([URL:domain], [NAME_1]).
_PLACEHOLDER = {
    "email": "[EMAIL]",
    "phone": "[PHONE]",
    "ssn": "[SSN]",
    "ip": "[IP]",
    "credit_card": "[CREDIT_CARD]",
    "path": "[PATH]",
    "url": "[URL:",
    "name": "[NAME_",
    "location": "[LOCATION]",
}


def placeholder_for(entity_type: str) -> str:
    """The scrub.py placeholder a given canonical type should produce."""
    if entity_type.startswith("secret/"):
        return "[SECRET]"
    return _PLACEHOLDER[entity_type]


class Case:
    def __init__(self, raw: dict, dimension: str, tier: str):
        self.id = raw["id"]
        self.dimension = dimension
        self.tier = tier
        self.surfaces = raw["surfaces"]
        self.input = raw["input"]
        self.sensitive = raw.get("sensitive", [])
        self.guard = raw.get("guard", [])
        self.raw = raw

    def applies_to(self, surface: str) -> bool:
        return surface in self.surfaces


def gap_surfaces(span: dict) -> set[str]:
    """Surfaces on which this span is a known, pinned gap (currently leaks)."""
    gap = span.get("known_gap")
    return set(gap.get("surfaces", [])) if gap else set()


def _validate(case: Case) -> None:
    text = case.input
    n = len(text)  # Python str length is codepoints — the corpus offset unit
    for span in case.sensitive:
        s, e = span["start_position"], span["end_position"]
        if not (0 <= s < e <= n):
            raise ValueError(f"{case.id}: span out of range {s}:{e} (len {n})")
        got = text[s:e]
        if got != span["entity_value"]:
            raise ValueError(
                f"{case.id}: offsets {s}:{e} -> {got!r} "
                f"!= entity_value {span['entity_value']!r}"
            )
        gap = span.get("known_gap")
        if gap is not None:
            extra = set(gap.get("surfaces", [])) - set(case.surfaces)
            if extra:
                raise ValueError(
                    f"{case.id}: known_gap.surfaces {extra} not in case.surfaces"
                )
            if gap.get("disposition") not in ("todo", "wontfix"):
                raise ValueError(f"{case.id}: bad known_gap.disposition")
    for g in case.guard:
        if g not in text:
            raise ValueError(f"{case.id}: guard {g!r} not present in input")


def load_corpus(tier: str = "deterministic") -> list[Case]:
    """Load + validate cases from manifest.json's dimension files.

    `tier` selects which dimensions to load. The deterministic tier is the
    strict cross-language CI gate (regex scrubber + worker gate); the `ner`
    tier is a separate, non-blocking, Python-only suite scored against the
    Presidio models pinned in manifest.ner_models. A dimension with no `tier`
    field defaults to deterministic.
    """
    manifest = json.loads((CORPUS_DIR / "manifest.json").read_text())
    cases: list[Case] = []
    for dim in manifest["dimensions"]:
        if dim.get("tier", "deterministic") != tier:
            continue
        path = CORPUS_DIR / dim["file"]
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            case = Case(json.loads(line), dim["id"], tier)
            _validate(case)
            cases.append(case)
    ids = [c.id for c in cases]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate case ids: {dupes}")
    return cases
