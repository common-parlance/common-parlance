"""Python runtime for the cp-scrub golden corpus (deterministic parity tier).

Runs scrub.py's RegexScrubber against the shared corpus and asserts the
ground truth: sensitive spans are redacted (or pinned as a known leak), and
guard substrings survive (over-redaction net). No Presidio needed — this tier
is the strict, CI-blocking cross-language gate. The NER tier is separate.
"""

import pytest

from common_parlance.scrub import RegexScrubber
from tests.golden_corpus import gap_surfaces, load_corpus, placeholder_for

_CASES = load_corpus()
_SCRUB_CASES = [c for c in _CASES if c.applies_to("scrub")]

_scrubber = RegexScrubber()


def test_corpus_has_scrub_cases():
    assert _SCRUB_CASES, "no scrub-surface cases loaded from corpus"


@pytest.mark.parametrize("case", _SCRUB_CASES, ids=lambda c: c.id)
def test_scrub_parity(case):
    out = _scrubber.scrub(case.input)

    for span in case.sensitive:
        val = span["entity_value"]
        if "scrub" in gap_surfaces(span):
            # Known gap: current scrub deliberately leaks this. Pin the leak so
            # CI stays green on documented behavior — and goes RED (good red)
            # the day someone closes the gap, prompting a corpus update.
            assert val in out, (
                f"{case.id}: known_gap expected a leak, but {val!r} was "
                f"redacted — gap closed? flip the corpus flag"
            )
        else:
            assert val not in out, f"{case.id}: leaked sensitive {val!r}"
            ph = placeholder_for(span["entity_type"])
            assert ph in out, f"{case.id}: expected placeholder {ph!r} in {out!r}"

    for g in case.guard:
        assert g in out, f"{case.id}: over-redacted guard {g!r} (now {out!r})"
