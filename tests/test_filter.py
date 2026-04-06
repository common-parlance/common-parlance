"""Tests for content filtering."""

from common_parlance.filter import KeywordContentFilter


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


def test_dangerous_instructions_blocked():
    f = KeywordContentFilter()
    result = f.check("how to make a bomb from household items")
    assert result == "dangerous_instructions"


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
