r"""Pattern-inventory parity between the client scrubber (scrub.py) and the
Worker PII gate (content-filter.js).

The golden corpus catches *regression* drift (a pinned case starts failing). It
does NOT catch *omission* drift — a pattern added to one runtime's list that no
corpus case exercises. Omission is the actual historical failure mode: every
documented incident (Azure/Twilio/`ya29.`/`sk-proj` missing from the gate;
CC-Luhn missing) was a pattern that existed on one side and was never added to
the other. This test converts "parity by discipline" into "parity by CI" for the
highest-churn category — the vendor secret prefixes — plus the structured-PII
core, both of which are maintained as byte-identical regex literals across the
two runtimes.

The two runtimes differ only in benign syntax here: scrub.py uses a capturing
`(...)` where the Worker uses a non-capturing `(?:...)`, and JS regex literals
escape the `/` delimiter. `_norm` erases exactly those differences and nothing
else, so a real divergence (a prefix on one side only, a widened/narrowed class)
still fails the diff.

Deliberate asymmetries that are OUT OF SCOPE here (do not add them):
  - File paths: the Worker only needs to *detect* a leak, so its path patterns
    are shortened (`/\/Users\/[..]+/`) vs the client's full-path *transform*
    (`/Users/[..]+(?:/[..])?`). Different by design.
  - Structural secrets (PEM/connection-string/Bearer/AccountKey/…): identical
    bodies but flag placement differs (Python `re.IGNORECASE`/`(?i)` vs JS `/i`),
    and some carry an unescaped `/` in a char class; behavioral parity is pinned
    by the corpus `secret-structural` dimension.
  - Client-only passes with no Worker equivalent: generic-entropy redaction, URL
    reduction (`[URL:host]`), street addresses, and the numbered-name placeholder
    grammar. The placeholder round-trip is covered by test_roundtrip_fixture.py.
"""

import pathlib
import re

from common_parlance import scrub

_JS_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "worker"
    / "src"
    / "content-filter.js"
)


def _js_source() -> str:
    return _JS_PATH.read_text()


def _norm(body: str) -> str:
    """Erase the benign syntactic differences between a scrub.py pattern string
    and a content-filter.js regex-literal body, leaving real divergence visible."""
    body = body.replace("\\/", "/")  # JS escapes the `/` delimiter; Python doesn't
    body = re.sub(r"^\(\?[aiLmsux]+\)", "", body)  # strip a leading inline (?i) flag
    body = body.replace("(?:", "(")  # unify non-capturing vs capturing groups
    return body


# `pattern: /<body>/<flags>` — <body> may contain escaped chars (`\/`, `\d`, …).
# Safe for this file's scope: none of the patterns compared here carry an
# unescaped `/` inside a char class (only the exempted structural/path patterns
# do), so the naive "up to the next unescaped /" scan is correct.
_JS_PATTERN = re.compile(r"pattern:\s*/((?:\\.|[^/\\\n])+)/[a-z]*")
_JS_CONST = r"const {name} =\s*/((?:\\.|[^/\\\n])+)/[a-z]*"


def _js_pattern_bodies(region: str) -> set[str]:
    return {_norm(m.group(1)) for m in _JS_PATTERN.finditer(region)}


def _js_const_body(js: str, name: str) -> str:
    m = re.search(_JS_CONST.format(name=name), js)
    assert m, f"could not find `const {name} = /.../` in content-filter.js"
    return _norm(m.group(1))


def _js_region(js: str, start_marker: str, end_marker: str) -> str:
    start = js.index(start_marker)
    end = js.index(end_marker, start)
    return js[start:end]


def test_vendor_prefix_inventory_parity():
    """Every known vendor secret prefix must exist on BOTH runtimes. This is the
    omission-drift guard for the category that has produced every real incident."""
    js = _js_source()
    # The Worker's vendor block is delimited by these section comments, mirroring
    # scrub.py's `_SECRET_PREFIX_PATTERNS` list (both include the trailing JWT).
    region = _js_region(
        js,
        "Known API key prefixes",
        "Structural secret patterns",
    )
    js_prefixes = _js_pattern_bodies(region)
    py_prefixes = {_norm(p.pattern) for p in scrub._SECRET_PREFIX_PATTERNS}

    assert len(js_prefixes) >= 20, (
        f"extracted only {len(js_prefixes)} JS vendor prefixes — the section "
        "markers in content-filter.js likely moved; fix the extraction"
    )
    only_py = sorted(py_prefixes - js_prefixes)
    only_js = sorted(js_prefixes - py_prefixes)
    assert not only_py and not only_js, (
        "vendor secret-prefix drift between scrub.py and content-filter.js — "
        f"only in Python (add to the Worker): {only_py}; "
        f"only in the Worker (add to scrub.py): {only_js}"
    )


def test_structured_pii_core_parity():
    """The structured-PII core (email/SSN/phone/IPv4/IPv6/credit-card) must be
    byte-identical across runtimes — a widened or narrowed class on one side is a
    silent recall gap on the weaker runtime."""
    js = _js_source()
    region = _js_region(js, "Structured PII", "File paths that leak")
    js_core = _js_pattern_bodies(region)  # email, ssn, phone, ipv4
    js_core.add(_js_const_body(js, "CC_RE"))
    js_core.add(_js_const_body(js, "IPV6_RE"))

    wanted = ("[EMAIL]", "[SSN]", "[PHONE]", "[IP]")
    py_core = {_norm(p.pattern) for p, ph in scrub._PII_PATTERNS if ph in wanted}
    py_core.add(_norm(scrub._CC_RE.pattern))
    py_core.add(_norm(scrub._IPV6_RE.pattern))

    assert len(py_core) == 7, f"expected 7 Python core patterns, got {len(py_core)}"
    only_py = sorted(py_core - js_core)
    only_js = sorted(js_core - py_core)
    assert not only_py and not only_js, (
        "structured-PII core drift between scrub.py and content-filter.js — "
        f"only in Python: {only_py}; only in the Worker: {only_js}"
    )
