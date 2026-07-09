"""The dictionary stopword set gates secret detection in BOTH runtimes (the
Python client and the JS worker). If the two copies drift, the weaker one
silently defines real-world recall. This test pins them byte-for-byte equal.
Regenerate both from the same source; never hand-edit one."""

import pathlib
import re

from common_parlance._stopwords import STOPWORDS

_JS_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "worker" / "src" / "stopwords.js"
)


def _load_js_stopwords() -> set[str]:
    text = _JS_PATH.read_text()
    # Extract the array literal passed to `new Set([...])` so header-comment
    # prose can't contaminate the parse, then pull the quoted words.
    m = re.search(r"new Set\(\s*\[(.*?)\]\s*\)", text, re.DOTALL)
    assert m, "could not find `new Set([...])` in stopwords.js"
    return set(re.findall(r'"([a-z]+)"', m.group(1)))


def test_python_and_js_stopwords_are_identical():
    js_words = _load_js_stopwords()
    py_words = set(STOPWORDS)
    only_py = py_words - js_words
    only_js = js_words - py_words
    assert not only_py and not only_js, (
        f"stopword drift: {len(only_py)} only in Python "
        f"(e.g. {sorted(only_py)[:5]}), {len(only_js)} only in JS "
        f"(e.g. {sorted(only_js)[:5]})"
    )
    assert len(py_words) == len(js_words)
