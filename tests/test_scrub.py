"""Tests for PII scrubbing (regex-only, no Presidio dependency needed)."""

from common_parlance.scrub import (
    RegexScrubber,
    normalize_text,
    scrub_secrets,
    scrub_urls,
)

# --- Unicode normalization (adversarial evasion defense) ---


def test_normalize_cyrillic_homoglyphs_not_mapped():
    """NFKC does NOT map Cyrillic homoglyphs to Latin — they are distinct chars.

    True homoglyph defense would require Unicode TR39 confusables mapping,
    which is a larger scope. NFKC catches fullwidth, mathematical, and
    compatibility variants but not cross-script homoglyphs.
    """
    # Cyrillic а (U+0430) stays as Cyrillic а — NFKC doesn't change it
    text = "Contact \u0430lice for details"
    result = normalize_text(text)
    assert "\u0430" in result  # Cyrillic а preserved (NFKC limitation)


def test_normalize_fullwidth_chars():
    """Fullwidth Latin characters (U+FF21-U+FF3A) should normalize to ASCII."""
    text = "\uff2a\uff4f\uff48\uff4e"  # Fullwidth "John"
    result = normalize_text(text)
    assert result == "John"


def test_normalize_zero_width_spaces():
    """Zero-width characters inserted mid-word to break tokenization."""
    text = "john\u200b.\u200bsmith\u200b@example.com"
    result = normalize_text(text)
    assert result == "john.smith@example.com"


def test_normalize_bidi_overrides():
    """Bidi override characters used to visually reverse text."""
    text = "SSN: \u202e9876-54-321\u202c"
    result = normalize_text(text)
    assert "9876-54-321" in result
    assert "\u202e" not in result
    assert "\u202c" not in result


def test_normalize_preserves_normal_text():
    """Normal ASCII text should pass through unchanged."""
    text = "The quick brown fox jumps over the lazy dog."
    assert normalize_text(text) == text


def test_normalize_multiple_invisible_chars():
    """Multiple invisible characters in sequence should all be stripped."""
    text = "secret\u200b\u200c\u200d\u2060password"
    result = normalize_text(text)
    assert result == "secretpassword"


def test_regex_scrubber_catches_fullwidth_email():
    """RegexScrubber catches emails with fullwidth chars after NFKC."""
    scrubber = RegexScrubber()
    # "john" with fullwidth ｊ (U+FF4A) instead of Latin j
    text = "Email \uff4aohn@example.com for info"
    result = scrubber.scrub(text)
    assert "[EMAIL]" in result
    assert "john@example.com" not in result


def test_regex_scrubber_catches_zero_width_evasion():
    """RegexScrubber should catch PII with zero-width chars inserted to evade regex."""
    scrubber = RegexScrubber()
    text = "Call 555\u200b-123\u200b-4567 tomorrow"
    result = scrubber.scrub(text)
    assert "[PHONE]" in result


def test_scrub_email():
    scrubber = RegexScrubber()
    text = "Contact me at john.smith@example.com for details."
    result = scrubber.scrub(text)
    assert "[EMAIL]" in result
    assert "john.smith@example.com" not in result


def test_scrub_phone():
    scrubber = RegexScrubber()
    text = "Call me at (555) 123-4567 tomorrow."
    result = scrubber.scrub(text)
    assert "[PHONE]" in result
    assert "555" not in result


def test_scrub_ssn():
    scrubber = RegexScrubber()
    text = "My SSN is 123-45-6789."
    result = scrubber.scrub(text)
    assert "[SSN]" in result
    assert "123-45-6789" not in result


def test_scrub_ip():
    scrubber = RegexScrubber()
    text = "Server is at 192.168.1.100 on port 8080."
    result = scrubber.scrub(text)
    assert "[IP]" in result
    assert "192.168.1.100" not in result


def test_no_false_positive_on_normal_text():
    scrubber = RegexScrubber()
    text = "The quick brown fox jumps over the lazy dog."
    result = scrubber.scrub(text)
    assert result == text


def test_multiple_pii_types():
    scrubber = RegexScrubber()
    text = "Email john@test.com or call 555-123-4567, SSN 123-45-6789."
    result = scrubber.scrub(text)
    assert "[EMAIL]" in result
    assert "[PHONE]" in result
    assert "[SSN]" in result
    assert "john@test.com" not in result


# --- Secret scanning ---


def test_scrub_openai_key():
    text = "My key is sk-abc123def456ghi789jkl012mno"
    result = scrub_secrets(text)
    assert "[SECRET]" in result
    assert "sk-abc123" not in result


def test_scrub_anthropic_key():
    text = "Key: sk-ant-api03-abc123def456ghi789jkl"
    result = scrub_secrets(text)
    assert "[SECRET]" in result
    assert "sk-ant-" not in result


def test_scrub_github_pat():
    text = "Token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijkl"
    result = scrub_secrets(text)
    assert "[SECRET]" in result
    assert "ghp_" not in result


def test_scrub_aws_access_key():
    text = "AWS key: AKIAIOSFODNN7EXAMPLE"
    result = scrub_secrets(text)
    assert "[SECRET]" in result
    assert "AKIA" not in result


def test_scrub_private_key():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAK..."
    result = scrub_secrets(text)
    assert "[SECRET]" in result
    assert "PRIVATE KEY" not in result


def test_scrub_connection_string():
    text = "Use postgres://admin:s3cret@db.example.com:5432/mydb"
    result = scrub_secrets(text)
    assert "[SECRET]" in result
    assert "s3cret" not in result


def test_scrub_bearer_token():
    text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload"
    result = scrub_secrets(text)
    assert "[SECRET]" in result


def test_high_entropy_token_scrubbed():
    # Random 32-char string with high entropy
    text = "Token: aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1v"
    result = scrub_secrets(text)
    assert "[SECRET]" in result


def test_low_entropy_string_preserved():
    # Repeated/simple string — should not be flagged
    text = "The value is aaaaaaaabbbbbbbbcccccccc"
    result = scrub_secrets(text)
    assert "aaaaaaaabbbbbbbbcccccccc" in result


# --- File path scrubbing ---


def test_scrub_macos_path():
    scrubber = RegexScrubber()
    text = "File at /Users/johndoe/Documents/report.pdf"
    result = scrubber.scrub(text)
    assert "[PATH]" in result
    assert "johndoe" not in result
    assert "Documents/report.pdf" not in result


def test_scrub_linux_path():
    scrubber = RegexScrubber()
    text = "Config in /home/alice/.config/app/settings.json"
    result = scrubber.scrub(text)
    assert "[PATH]" in result
    assert "alice" not in result
    assert ".config/app/settings.json" not in result


def test_scrub_windows_path():
    scrubber = RegexScrubber()
    text = r"Located at C:\Users\bob\Desktop\file.txt"
    result = scrubber.scrub(text)
    assert "[PATH]" in result
    assert "bob" not in result
    assert "Desktop" not in result


def test_scrub_path_with_trailing_text():
    """Full path is scrubbed but surrounding text is preserved."""
    scrubber = RegexScrubber()
    text = "saved at /Users/johndoe/Projects/acme-corp/app.py, then opened it"
    result = scrubber.scrub(text)
    assert "[PATH]" in result
    assert "johndoe" not in result
    assert "acme-corp" not in result
    assert "then opened it" in result


def test_scrub_path_username_only():
    """Path with just the username (no deeper path) still works."""
    scrubber = RegexScrubber()
    text = "the /Users/johndoe directory"
    result = scrubber.scrub(text)
    assert "[PATH]" in result
    assert "johndoe" not in result
    assert "directory" in result


# --- Credit card (Luhn checksum) ---


def test_scrub_valid_visa():
    scrubber = RegexScrubber()
    # 4111 1111 1111 1111 is a well-known Visa test number (passes Luhn)
    text = "Card: 4111 1111 1111 1111"
    result = scrubber.scrub(text)
    assert "[CREDIT_CARD]" in result
    assert "4111" not in result


def test_scrub_valid_mastercard():
    scrubber = RegexScrubber()
    # 5500 0000 0000 0004 is a Mastercard test number (passes Luhn)
    text = "Pay with 5500 0000 0000 0004"
    result = scrubber.scrub(text)
    assert "[CREDIT_CARD]" in result
    assert "5500" not in result


def test_scrub_card_with_dashes():
    scrubber = RegexScrubber()
    text = "Card number: 4111-1111-1111-1111"
    result = scrubber.scrub(text)
    assert "[CREDIT_CARD]" in result


def test_no_false_positive_on_random_digits():
    scrubber = RegexScrubber()
    # 16 digits that fail Luhn — should NOT be scrubbed
    text = "Order number 1234567890123456"
    result = scrubber.scrub(text)
    assert "1234567890123456" in result


def test_no_false_positive_on_short_numbers():
    scrubber = RegexScrubber()
    text = "The year is 2026 and there are 8192 items."
    result = scrubber.scrub(text)
    assert result == text


# --- URL reduction ---


def test_scrub_url_preserves_domain():
    result = scrub_urls("Check https://docs.python.org/3/library/re.html for details")
    assert result == "Check [URL:docs.python.org] for details"


def test_scrub_url_strips_query_and_path():
    result = scrub_urls("Visit https://example.com/page?user=alice&token=abc123")
    assert "[URL:example.com]" in result
    assert "alice" not in result
    assert "abc123" not in result


def test_scrub_url_strips_credentials():
    result = scrub_urls("Use https://admin:s3cret@internal.corp.com/api")
    assert "[URL:internal.corp.com]" in result
    assert "admin" not in result
    assert "s3cret" not in result


def test_scrub_url_flags_spam_tld():
    result = scrub_urls("Go to https://free-prizes.xyz/claim-now")
    assert "[URL:suspicious]" in result
    assert "free-prizes" not in result


def test_scrub_url_http():
    result = scrub_urls("See http://localhost:8080/health for status")
    assert "[URL:localhost]" in result


def test_scrub_no_false_positive_on_plain_text():
    text = "The ratio is 3:1 and the time is 10:30."
    result = scrub_urls(text)
    assert result == text


def test_scrub_url_integrated_in_scrubber():
    scrubber = RegexScrubber()
    text = "API docs at https://api.example.com/v2/users?key=secret123"
    result = scrubber.scrub(text)
    assert "[URL:api.example.com]" in result
    assert "secret123" not in result
