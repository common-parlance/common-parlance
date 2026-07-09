"""Freshness guard for the round-trip parity fixture.

The cross-language invariant itself is asserted by the Worker
(worker/test/roundtrip-pii.test.js). This test only pins the committed fixture
equal to what the current scrubber produces, so it can never silently go stale:
if scrub.py's output changes and the fixture isn't regenerated, the Worker would
be asserting against outdated scrubbed text.

Regenerate: python scripts/regen_roundtrip_fixture.py --write
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import regen_roundtrip_fixture as rf  # noqa: E402


def test_fixture_is_fresh():
    assert rf.FIXTURE.exists(), (
        "round-trip fixture missing — run: "
        "python scripts/regen_roundtrip_fixture.py --write"
    )
    committed = rf.FIXTURE.read_text()
    regenerated = rf.render(rf.build_fixture())
    assert committed == regenerated, (
        "round-trip fixture is stale — scrub.py output changed. Regenerate: "
        "python scripts/regen_roundtrip_fixture.py --write"
    )


def test_fixture_nonempty():
    assert rf.build_fixture(), "no round-trip cases generated from the corpus"
