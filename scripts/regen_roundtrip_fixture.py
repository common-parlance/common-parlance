"""Generate the cross-language round-trip parity fixture.

For every deterministic-tier corpus input, this records the client
`RegexScrubber`'s output. The Worker test (worker/test/roundtrip-pii.test.js)
loads the fixture and asserts `checkPii()` returns null on each — i.e. a
correctly client-scrubbed upload is never rejected by the server gate.

That invariant is not covered elsewhere: the golden corpus pins behavioral
parity (raw input -> expected verdict on each side independently), and
test_pattern_inventory_parity.py pins definition parity (the regex literals).
Neither exercises the actual cross-language property that the hand-synced
placeholder grammar depends on — the client's emit vocabulary (`[URL:host]`,
`[NAME_1]`, `[EMAIL]`, …) vs the Worker's PII_ALLOWLIST. If the scrubber emits a
placeholder the gate doesn't allowlist (e.g. `[URL:192.168.1.1]`, whose inner IP
would trip the IPv4 detector), an honest upload bounces; this fixture makes that
a failing test.

Cases pinned as a `scrub`-surface known-gap are excluded: the client
deliberately does not fully scrub them, so their output legitimately still
carries a sensitive value and the gate SHOULD reject it — they are not part of
the "correctly scrubbed" set.

Usage:
    # verify the committed fixture matches a fresh regeneration
    python scripts/regen_roundtrip_fixture.py --check
    # rewrite the fixture
    python scripts/regen_roundtrip_fixture.py --write
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))  # make `common_parlance` and `tests` importable

from common_parlance.scrub import RegexScrubber  # noqa: E402
from tests.golden_corpus import gap_surfaces, load_corpus  # noqa: E402

FIXTURE = _REPO / "worker" / "test" / "fixtures" / "scrubbed_corpus.json"


def build_fixture() -> list[dict]:
    scrubber = RegexScrubber()
    rows: list[dict] = []
    for case in load_corpus():  # deterministic tier — the shared cross-language gate
        if any("scrub" in gap_surfaces(sp) for sp in case.sensitive):
            continue  # client deliberately leaks this; not "correctly scrubbed"
        rows.append({"id": case.id, "scrubbed": scrubber.scrub(case.input)})
    rows.sort(key=lambda r: r["id"])
    return rows


def render(rows: list[dict]) -> str:
    return json.dumps(rows, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--check", action="store_true", help="verify committed == regenerated"
    )
    group.add_argument("--write", action="store_true", help="rewrite the fixture")
    args = ap.parse_args()

    text = render(build_fixture())

    if args.write:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(text)
        print(f"wrote {FIXTURE.relative_to(_REPO)}")
        return 0

    if not FIXTURE.exists():
        print("fixture missing — run: scripts/regen_roundtrip_fixture.py --write")
        return 1
    if FIXTURE.read_text() != text:
        print("fixture STALE — run: scripts/regen_roundtrip_fixture.py --write")
        return 1
    print("fixture up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
