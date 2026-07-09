"""Tests for language detection (py3langid fallback only)."""

import pytest

from common_parlance import lang
from common_parlance.lang import detect_language


def test_detect_english():
    text = (
        "Python is a programming language that lets you work quickly"
        " and integrate systems more effectively."
    )
    assert detect_language(text) == "en"


def test_detect_french():
    text = (
        "Python est un langage de programmation qui permet de"
        " travailler rapidement et d'intégrer les systèmes"
        " plus efficacement."
    )
    assert detect_language(text) == "fr"


def test_detect_spanish():
    text = (
        "Python es un lenguaje de programación que permite trabajar"
        " de forma rápida e integrar sistemas de manera efectiva."
    )
    assert detect_language(text) == "es"


def test_empty_returns_unknown():
    assert detect_language("") == "unknown"


def test_short_gibberish_returns_something():
    # Very short text may return any language or unknown — just
    # verify it doesn't crash
    result = detect_language("asdf")
    assert isinstance(result, str)


def test_model_checksum_rejects_poisoned_cache(tmp_path, monkeypatch):
    """A cached model that fails the pinned SHA-256 is rejected and removed."""
    monkeypatch.setattr(lang, "_CACHE_DIR", tmp_path)
    poisoned = tmp_path / "lid.176.ftz"
    poisoned.write_bytes(b"not the real model")

    with pytest.raises(RuntimeError, match="checksum"):
        lang._get_model_path()

    # The bad file is removed so a re-run can re-download cleanly.
    assert not poisoned.exists()
