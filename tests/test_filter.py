"""Tests for content filtering."""

import common_parlance.filter_ml as filter_ml
from common_parlance.filter import (
    CompositeContentFilter,
    KeywordContentFilter,
    create_content_filter,
)


def test_safe_content_passes():
    f = KeywordContentFilter()
    assert f.check("How do I sort a list in Python?") is None


def test_safe_content_with_common_words():
    """Words like 'child' or 'minor' in normal context should pass."""
    f = KeywordContentFilter()
    assert f.check("The child process exited with code 0") is None
    assert f.check("This is a minor issue in the codebase") is None
    assert f.check("She made a bomb cake for the party") is None


def test_csam_indicators_blocked():
    f = KeywordContentFilter()
    result = f.check("something about child exploitation material")
    assert result == "csam_indicator"


def test_csam_separator_evasion_blocked():
    # \s* (not \s+) between keywords catches the separator-collapse form
    # ("child.porn" -> "childporn") and bare concatenation, without
    # over-blocking benign uses of the same words.
    f = KeywordContentFilter()
    for evasion in ("child.porn", "childporn", "child_porn", "minor.sex"):
        assert f.check(evasion) == "csam_indicator", evasion
    assert f.check("child safety education") is None
    assert f.check("The child process exited") is None


def test_dangerous_instructions_blocked():
    f = KeywordContentFilter()
    result = f.check("how to make a bomb from household items")
    assert result == "dangerous_instructions"


def test_dangerous_instructions_an_article():
    # (?:a\s+)? could not match "an "; (?:an?\s+)? matches both a/an.
    f = KeywordContentFilter()
    assert f.check("how to build an explosive") == "dangerous_instructions"
    assert f.check("how to create an anthrax weapon") == "dangerous_instructions"


def test_case_insensitive():
    f = KeywordContentFilter()
    assert f.check("How To Make A Bomb") == "dangerous_instructions"
    assert f.check("CHILD EXPLOITATION") == "csam_indicator"


def test_empty_string_passes():
    f = KeywordContentFilter()
    assert f.check("") is None


def test_normal_conversation_passes():
    f = KeywordContentFilter()
    text = (
        "Can you explain how neural networks work? "
        "I'm trying to understand backpropagation and gradient descent."
    )
    assert f.check(text) is None


# --- Leetspeak evasion ---


def test_leetspeak_csam_blocked():
    f = KeywordContentFilter()
    assert f.check("ch1ld p0rn") == "csam_indicator"


def test_leetspeak_dangerous_blocked():
    f = KeywordContentFilter()
    assert f.check("h0w 7o m4k3 a b0mb") == "dangerous_instructions"


def test_separator_evasion_blocked():
    f = KeywordContentFilter()
    assert f.check("child p*o*r*n") == "csam_indicator"


def test_mixed_leet_and_separators():
    f = KeywordContentFilter()
    assert f.check("ch1ld 3xpl01t4t10n") == "csam_indicator"


# --- Single-character-spacing evasion ---


def test_single_char_spacing_csam_blocked():
    f = KeywordContentFilter()
    assert f.check("c h i l d p o r n") == "csam_indicator"
    assert f.check("p e d o p h i l e") == "csam_indicator"


def test_single_char_spacing_combined_with_leet():
    # spacing + leet (1->i, 0->o) in the same run
    f = KeywordContentFilter()
    assert f.check("c h 1 l d p 0 r n") == "csam_indicator"


def test_single_char_spacing_does_not_overblock():
    # Only *runs of single chars* collapse, and a collapsed benign run
    # ("U S A" -> "USA") doesn't form a blocklist term. Normal word spacing
    # must stay untouched so legit text isn't fused into a match.
    f = KeywordContentFilter()
    assert f.check("the grades were A B C D F") is None
    assert f.check("it stands for U S A") is None
    assert f.check("child safety education") is None
    assert f.check("a minor change to the code") is None


def test_single_char_spacing_no_redos_on_long_input():
    # Bounded {1,3}/{2,} quantifiers — a long separator run must not
    # catastrophically backtrack. Completes fast and blocks nothing.
    f = KeywordContentFilter()
    assert f.check("a " * 10000) is None
    assert f.check("a." * 10000) is None


# --- Combining-mark (Zalgo) evasion ---


def test_combining_mark_evasion_blocked():
    # NFKC keeps combining marks; the NFD + mark-drop step removes them so the
    # base letters re-form the term. "child porn" with a combining acute
    # (U+0301) stacked on each base letter.
    f = KeywordContentFilter()
    zalgo = "ćh́íĺd́ ṕóŕń"
    assert f.check(zalgo) == "csam_indicator"


def test_combining_mark_does_not_overblock_accents():
    # Stripping accents for the match key must not invent a blocklist hit.
    f = KeywordContentFilter()
    assert f.check("a café résumé about naïve algorithms") is None


# --- Bidi-isolate evasion (Trojan-Source class, CVE-2021-42574) ---


def test_bidi_isolate_evasion_blocked():
    # The bidi isolates U+2066-2069 are now stripped by normalize_text, so a
    # term split by one (with no other separator) still re-forms and matches.
    f = KeywordContentFilter()
    assert f.check("child⁦porn") == "csam_indicator"
    assert f.check("how to make a b⁨omb") == "dangerous_instructions"


# --- Composite filter + ML layering ---


class _StubFilter:
    """A content filter that returns a fixed reason and records being called."""

    def __init__(self, reason=None):
        self.reason = reason
        self.called = False

    def check(self, text):
        self.called = True
        return self.reason


def test_composite_returns_first_block_and_short_circuits():
    first = _StubFilter("blocked_a")
    second = _StubFilter("blocked_b")
    cf = CompositeContentFilter([first, second])
    assert cf.check("x") == "blocked_a"
    # The expensive later filter is never consulted once an earlier one blocks.
    assert first.called
    assert not second.called


def test_composite_falls_through_to_later_filter():
    keyword = _StubFilter(None)
    ml = _StubFilter("ml_toxicity")
    cf = CompositeContentFilter([keyword, ml])
    assert cf.check("x") == "ml_toxicity"
    assert keyword.called and ml.called


def test_composite_all_clear_returns_none():
    cf = CompositeContentFilter([_StubFilter(None), _StubFilter(None)])
    assert cf.check("hello world") is None


def test_factory_use_ml_false_is_keyword_only():
    cf = create_content_filter(use_ml=False)
    assert isinstance(cf, KeywordContentFilter)


def test_factory_degrades_when_detoxify_absent(monkeypatch):
    # create_ml_content_filter returns None when detoxify isn't installed.
    monkeypatch.setattr(filter_ml, "create_ml_content_filter", lambda *a, **k: None)
    cf = create_content_filter(use_ml=True)
    assert isinstance(cf, KeywordContentFilter)


def test_factory_layers_ml_when_available(monkeypatch):
    monkeypatch.setattr(
        filter_ml,
        "create_ml_content_filter",
        lambda *a, **k: _StubFilter("ml_toxicity"),
    )
    cf = create_content_filter(use_ml=True)
    assert isinstance(cf, CompositeContentFilter)
    # Keyword runs first; clean text then reaches the ML layer.
    assert cf.check("perfectly normal text") == "ml_toxicity"
