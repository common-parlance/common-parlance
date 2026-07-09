"""The content filter's hand-duplicated lookup data — the cross-script
confusables (homoglyph) map and the invisible/bidi-control codepoint set — is
maintained separately in the Python client (filter.py / scrub.py) and the JS
worker (content-filter.js). If the two copies drift, the weaker runtime silently
defines real-world coverage (the same class of bug that let C0-separated and
homoglyph evasions through one gate but not the other). These tests pin the data
equal across runtimes until a shared-source/codegen mechanism replaces the
hand-copies; never hand-edit one side alone.
"""

import pathlib
import re

from common_parlance import scrub
from common_parlance.filter import _CONFUSABLES

_JS_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "worker"
    / "src"
    / "content-filter.js"
)


def _js_source() -> str:
    return _JS_PATH.read_text()


def _codepoints_in_class(pattern: str) -> set[int]:
    """Codepoints inside the [...] of a compiled regex char class (the Python
    sources interpolate \\uXXXX escapes, so the pattern holds literal chars)."""
    inner = re.search(r"\[(.*)\]", pattern, re.DOTALL)
    assert inner, "no character class found in pattern"
    return {ord(c) for c in inner.group(1)}


def test_confusables_map_parity():
    js = _js_source()
    block = re.search(r"const CONFUSABLES = \{(.*?)\};", js, re.DOTALL)
    assert block, "could not find CONFUSABLES object in content-filter.js"
    js_conf = dict(re.findall(r'([^\s,]):\s*"([A-Za-z])"', block.group(1)))
    py_conf = {chr(o): v for o, v in _CONFUSABLES.items()}

    only_py = {k: py_conf[k] for k in py_conf.keys() - js_conf.keys()}
    only_js = {k: js_conf[k] for k in js_conf.keys() - py_conf.keys()}
    mismatched = {
        k: (py_conf[k], js_conf[k])
        for k in py_conf.keys() & js_conf.keys()
        if py_conf[k] != js_conf[k]
    }
    assert not only_py and not only_js and not mismatched, (
        f"confusables drift — only in Python: {only_py}, only in JS: {only_js}, "
        f"mismatched values: {mismatched}"
    )


def test_invisible_set_parity():
    js = _js_source()
    line = re.search(r"const INVISIBLE_RE =\s*/\[(.*?)\]\+/g", js, re.DOTALL)
    assert line, "could not find INVISIBLE_RE in content-filter.js"
    js_inv = {int(h, 16) for h in re.findall(r"\\u([0-9a-fA-F]{4})", line.group(1))}
    py_inv = _codepoints_in_class(scrub._INVISIBLE_RE.pattern)

    only_py = sorted(hex(c) for c in py_inv - js_inv)
    only_js = sorted(hex(c) for c in js_inv - py_inv)
    assert py_inv == js_inv, (
        f"invisible/bidi set drift — only in Python: {only_py}, only in JS: {only_js}"
    )
