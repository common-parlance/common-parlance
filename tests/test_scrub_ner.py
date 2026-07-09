"""Tests for the Presidio NER path: numbered, coreference-preserving names.

Skipped automatically if presidio or the spaCy model isn't installed, so the
regex-only test suite still runs in minimal environments.
"""

import pytest

pytest.importorskip("presidio_analyzer")

from common_parlance.scrub import PresidioScrubber  # noqa: E402


@pytest.fixture(scope="module")
def scrubber():
    try:
        return PresidioScrubber()
    except Exception as exc:  # spaCy model not downloaded, etc.
        pytest.skip(f"PresidioScrubber unavailable: {exc}")


def test_person_gets_numbered_placeholder(scrubber):
    out = scrubber.scrub("Alice Johnson met Bob Smith at the office.")
    assert "[NAME_1]" in out
    assert "[NAME_2]" in out
    assert "Alice" not in out and "Bob" not in out


def test_repeated_name_is_coreference_consistent(scrubber):
    # Same person mentioned twice -> same numbered placeholder; distinct
    # person -> a different number. This is the documented design rationale.
    out = scrubber.scrub("Alice Johnson called Bob Smith, then Alice Johnson left.")
    assert out.count("[NAME_1]") == 2  # Alice (first in reading order)
    assert "[NAME_2]" in out  # Bob
    assert "Alice" not in out and "Bob" not in out


def test_url_domain_preserved_not_double_scrubbed(scrubber):
    # URLs are reduced to [URL:domain] by the regex pass that runs before
    # Presidio. Presidio must NOT re-scrub the bare domain inside the
    # placeholder — that double-pass produced a malformed "[URL:[URL]]" and
    # dropped the domain (URL was in the Presidio entity map). Regression.
    out = scrubber.scrub("hit https://example.com/users now")
    assert out == "hit [URL:example.com] now", out
