"""Tests for language detection (langdetect fallback only)."""

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
