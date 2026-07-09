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


def test_scrub_international_phone():
    # +<country code> E.164-style — the US NANP regex misses these; the
    # +-anchored international pattern catches them (validated 0%→100% on seeded
    # real-code recall). Leading-0 national / parenthesized formats are out of scope.
    scrubber = RegexScrubber()
    for num in ["+44 20 7946 0958", "+49 30 12345678", "+81 3 1234 5678"]:
        out = scrubber.scrub(f"reach me at {num} anytime")
        assert num not in out and "[PHONE]" in out, out


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


def test_scrub_no_catastrophic_backtracking():
    # Bounded quantifiers (email local-part, connection-string scheme, credit
    # card separators) must keep these pathological inputs linear, not O(n^2).
    import time

    scrubber = RegexScrubber()
    for payload in ("4-" * 50000 + "x", "a." * 100000 + " ", "a://" * 50000):
        t = time.time()
        scrubber.scrub(payload)
        assert time.time() - t < 2.0, f"slow on {payload[:8]!r}..."


def test_scrub_zero_padded_ip():
    # Zero-padded octets (common in firewall/router and Windows logs) are valid
    # IPs and must be redacted — the octet-range alternation rejected the
    # leading zero, leaking the raw address to the public dataset.
    scrubber = RegexScrubber()
    for ip in ("192.168.001.001", "010.000.000.001"):
        result = scrubber.scrub(f"client {ip} connected")
        assert "[IP]" in result, ip
        assert ip not in result, ip


def test_scrub_connection_string_evasions():
    # Two regressions where credential URLs survived scrubbing. Hosts are
    # dot-less so the email pattern can't claim the "user@host" — this isolates
    # the connection-string fix from incidental email redaction.
    scrubber = RegexScrubber()
    # (1) password containing '/' (e.g. base64) bypassed the [^\s@/] class.
    assert "pa/ss123" not in scrubber.scrub("redis://admin:pa/ss123@localhost:6379")
    # (2) uppercase/mixed-case scheme bypassed the lowercase-anchored regex.
    assert "hunter2" not in scrubber.scrub("conn SMTP://admin:hunter2@localhost/x")
    # A plain lowercase DSN must still be redacted (no regression).
    assert "secret" not in scrubber.scrub("postgres://user:secret@host:5432/db")


def test_scrub_long_high_entropy_blob_caught_via_prefix():
    # A high-entropy secret blob longer than the entropy cost-cap (4096) used to
    # be SKIPPED entirely (cost guard), letting an inline secret bypass the
    # backstop. It's now evaluated via a bounded prefix and redacted.
    import base64
    import hashlib

    raw = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(300))
    blob = base64.b64encode(raw).decode().rstrip("=")
    assert len(blob) > 4096
    scrubber = RegexScrubber()
    result = scrubber.scrub(f"token {blob} end")
    assert "[SECRET]" in result
    assert blob not in result


def test_four_part_version_not_flagged_as_ip():
    # Octet-range validation: a build/version string with a component > 255 is
    # not a valid IPv4 and must be preserved, not redacted to [IP].
    scrubber = RegexScrubber()
    for ver in ("1.0.2403.1", "10.0.19041.508"):
        result = scrubber.scrub(f"upgrade to version {ver} today")
        assert "[IP]" not in result, ver
        assert ver in result, ver
    # A 5-part dotted number must not have a 4-octet window flagged.
    assert "[IP]" not in scrubber.scrub("rev 100.200.50.25.300 ok")


def test_ip_at_end_of_sentence_still_redacted():
    # (?!\.?\d) allows a trailing sentence period while rejecting a 5th octet,
    # so an IP that ends a sentence is still redacted (it used to leak).
    scrubber = RegexScrubber()
    assert "[IP]" in scrubber.scrub("The server is at 8.8.8.8.")
    assert "8.8.8.8" not in scrubber.scrub("contact 8.8.8.8, please")
    assert "[IP]" in scrubber.scrub("see 1.2.3.4:8080 for status")


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


def test_scrub_pkcs8_private_key():
    # Plain PKCS#8 header (no key-type word) — the modern default and GCP
    # service-account format. The old pattern required a key-type word
    # (RSA/EC/...) between BEGIN and PRIVATE and missed this entirely.
    text = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg..."
    result = scrub_secrets(text)
    assert "[SECRET]" in result
    assert "BEGIN PRIVATE KEY" not in result


def test_scrub_gcp_service_account_private_key_id():
    # GCP service-account JSON: private_key (PEM) and client_email are covered
    # elsewhere; this guards the 40-hex private_key_id fingerprint that no
    # other pass catches. Generic project_id/client_id are left untouched.
    text = '"private_key_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"'
    result = scrub_secrets(text)
    assert "[SECRET]" in result
    assert "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0" not in result


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


# --- Expanded vendor / structural secret coverage (Phase 0 hardening) ---


def test_scrub_openai_project_key():
    # sk-proj- keys contain a hyphen that the old pattern stopped at
    text = "OPENAI_API_KEY=sk-proj-abcdEFGH1234ijklMNOP5678"
    assert "[SECRET]" in scrub_secrets(text)
    assert "sk-proj-abcdEFGH1234ijklMNOP5678" not in scrub_secrets(text)


def test_scrub_github_finegrained_pat():
    text = "token github_pat_11ABCDEFG0" + "a" * 60
    assert "[SECRET]" in scrub_secrets(text)


def test_scrub_google_oauth_token():
    text = "Authorization uses ya29.A0ARrdaM-longtokenvaluehere1234567890"
    assert "[SECRET]" in scrub_secrets(text)


def test_scrub_twilio_account_sid():
    text = "Twilio('AC0123456789abcdef0123456789abcdef', token)"
    assert "[SECRET]" in scrub_secrets(text)


def test_scrub_azure_account_key():
    text = (
        "AccountName=foo;AccountKey="
        + "abcdEFGH1234567890" * 3
        + "==;EndpointSuffix=core.windows.net"
    )
    result = scrub_secrets(text)
    assert "[SECRET]" in result
    assert "abcdEFGH1234567890abcdEFGH1234567890" not in result


def test_scrub_presigned_url_signature():
    # A bare presigned-URL signature param — outside a scheme:// URL that
    # scrub_urls would reduce — is redacted as a secret, keeping the client in
    # parity with the Worker gate. (A full signed URL is reduced to [URL:host]
    # by scrub_urls upstream; this is the backstop for a stray signature param.)
    hexsig = "fedcba9876543210" * 4
    assert scrub_secrets(f"X-Amz-Signature={hexsig}") == "[SECRET]"
    azsig = "AbCdEf1234567890" * 3 + "Xyz%3D"
    assert "[SECRET]" in scrub_secrets(f"...&sig={azsig}")
    # Too-short sig= (the generic param) is left alone — no false positive.
    assert scrub_secrets("?sig=abc123") == "?sig=abc123"
    # A full-size GCS V4 RSA signature (512 hex for a 2048-bit key) is redacted
    # whole — no truncated tail left in the clear (the {16,512} value bound).
    rsa = "deadbeef" * 64  # 512 hex chars
    out = scrub_secrets(f"X-Goog-Signature={rsa}")
    assert out == "[SECRET]", out
    assert rsa[256:] not in out


def test_scrub_standalone_base64_blob():
    # base64 with +/= that the generic [a-zA-Z0-9_-] pattern can't match whole
    text = "blob: TWFueSBoYW5kcyBtYWtlIGxpZ2h0IHdvcmsgYW5kIG1vcmU=="
    assert "[SECRET]" in scrub_secrets(text)


def test_scrub_medium_base64_with_special_chars():
    # ~30-char high-entropy base64 with +/ — the generic 24-char pattern
    # fragments on the +/, so the dedicated base64 pass (>=28) must catch it.
    text = "k=aZ9bX2cV5nM8qW1eR4tY7+uI0oP3sD="
    assert "[SECRET]" in scrub_secrets(text)


def test_scrub_contextual_auth_token():
    # No vendor prefix (bare 32-hex) — caught via the key name, not blanket hex.
    text = "TWILIO_AUTH_TOKEN=0123456789abcdef0123456789abcdef"
    assert "[SECRET]" in scrub_secrets(text)


def test_scrub_dotted_encoded_token():
    # '.' splits the plain base64 pass; the dotted path catches it because it
    # also carries + / payload chars.
    text = "t=AbCdEfGhIjKlMn.OpQrStUvWxYz012345+/="
    assert "[SECRET]" in scrub_secrets(text)


def test_dotted_identifier_not_redacted():
    # Plain dotted identifier (no + or /, no 24+ char segment) must NOT be
    # redacted by the dotted path — common in code.
    text = "import com.fasterxml.jackson.databind.ObjectMapper"
    out = scrub_secrets(text)
    assert "[SECRET]" not in out
    assert "com.fasterxml.jackson.databind.ObjectMapper" in out


def test_secret_keyword_in_prose_not_redacted():
    # "secret" in prose without an assignment must not trigger redaction.
    text = "the secret to a good launch is shipping something people want"
    assert "[SECRET]" not in scrub_secrets(text)


def test_git_sha_not_flagged_as_secret():
    # 40-char hex commit hash sits at ~4.0 entropy — must be preserved
    text = "commit 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b fixed the bug"
    result = scrub_secrets(text)
    assert "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b" in result
    assert "[SECRET]" not in result


def test_code_variable_names_not_over_redacted():
    # Coding-dataset false-positive guard: short identifiers stay intact
    text = "let apiKey = userInput; const token = nextToken;"
    result = scrub_secrets(text)
    assert "[SECRET]" not in result


def test_long_camelcase_identifiers_not_over_redacted():
    text = (
        "Use AbstractSingletonProxyFactoryBean with "
        "ObjectMapperFactoryConfig in the container."
    )
    result = scrub_secrets(text)
    assert "[SECRET]" not in result
    assert "AbstractSingletonProxyFactoryBean" in result
    assert "ObjectMapperFactoryConfig" in result


def test_generic_high_entropy_requires_digit():
    # Pure-letter high-entropy identifiers are common in code and should not be
    # scrubbed by the generic fallback without stronger secret context.
    text = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    result = scrub_secrets(text)
    assert "[SECRET]" not in result
    assert text in result


def test_generic_high_entropy_with_digit_still_scrubbed():
    text = "Token: aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1v"
    result = scrub_secrets(text)
    assert "[SECRET]" in result
    assert "aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1v" not in result


def test_bare_aws_secret_key_shaped_token_scrubbed():
    # An AWS-secret-key-shaped token (no vendor prefix) is the case the old
    # vowel heuristic mis-classified as benign: its incidental letter runs
    # (Jalr, FEMI, ...) are not dictionary words, so the dictionary rejector
    # leaves it to the entropy pass, which redacts it.
    for tok in (
        "wJalrXUtnFEMIK7MDENGbPxRfiCYEX1aB2cD",
        "aZ9bX2cV5nM8qW1eR4tY7uI0oP3sDfG6h",
    ):
        result = scrub_secrets(f"the key is {tok} ok")
        assert "[SECRET]" in result, tok
        assert tok not in result, tok


def test_dictionary_words_cannot_smuggle_an_allcaps_secret_tail():
    # A secret tail padded with real words ("WordsThenSECRET") used to be
    # excused as benign because the words covered >=50% of the token; a long
    # non-dictionary run now disqualifies it, so the entropy pass redacts it.
    tok = "ConfigurationManagementInformationResourceZMDENGBPXRFICYEX9"
    result = scrub_secrets(f"key={tok}")
    assert "[SECRET]" in result, tok
    assert tok not in result, tok


def test_word_structured_token_with_digits_preserved():
    # The precision side of the same rejector: a token that IS mostly real
    # dictionary words (even with digits) must survive — these are real
    # identifiers, not secrets.
    for tok in (
        "WrappedResourceManager",
        "getUserByIdentifierFromCache",
        "config2ResourceLoaderFactory",
    ):
        result = scrub_secrets(f"call {tok} now")
        assert "[SECRET]" not in result, tok
        assert tok in result, tok


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


def test_scrub_non_http_url_schemes():
    # Non-http(s) URLs carry private filenames, repo names, and tokens in their
    # paths just like http URLs, so they must be reduced to [URL:host] too (the
    # optional Presidio pass no longer recognizes URLs). DB/connection-string
    # schemes are intentionally NOT reduced here — owned by the secret pattern.
    cases = [
        ("ftp://files.example.com/private/med.pdf", "[URL:files.example.com]"),
        ("git://github.com/acme-corp/secret-roadmap", "[URL:github.com]"),
        ("s3://prod-bucket/exports/payroll.csv", "[URL:prod-bucket]"),
        ("sftp://user@host.internal/dump.sql", "[URL:host.internal]"),
    ]
    for url, expected in cases:
        out = scrub_urls(f"see {url} now")
        assert out == f"see {expected} now", out


def test_scrub_db_connection_url_left_for_secret_pattern():
    # postgres/redis style URLs must NOT be URL-reduced (the secret pattern
    # handles their credentials and preserves credential-less service URLs).
    scrubber = RegexScrubber()
    # credential connstring: creds gone, but not turned into [URL:host]
    out = scrubber.scrub("db at postgres://admin:s3cret@db.internal:5432/app")
    assert "s3cret" not in out
    assert "[URL:" not in out


def test_scrub_url_strips_credentials():
    result = scrub_urls("Use https://admin:s3cret@internal.corp.com/api")
    assert "[URL:internal.corp.com]" in result
    assert "admin" not in result
    assert "s3cret" not in result


def test_scrub_url_flags_spam_tld():
    result = scrub_urls("Go to https://free-prizes.click/claim-now")
    assert "[URL:suspicious]" in result
    assert "free-prizes" not in result


def test_scrub_url_does_not_flag_legit_dropped_tld():
    # .xyz hosts substantial legitimate use (e.g. abc.xyz) and was dropped from
    # the spam list — its host must be preserved, not rewritten to suspicious.
    result = scrub_urls("Read the docs at https://docs.example.xyz/guide")
    assert "[URL:docs.example.xyz]" in result
    assert "[URL:suspicious]" not in result


def test_scrub_url_does_not_flag_brandable_gtlds():
    # .shop and .vip are mainstream commerce gTLDs (huge legit use); flagging
    # them would rewrite real storefront/brand links.
    for host in ("mystore.shop", "brand.vip"):
        result = scrub_urls(f"buy at https://{host}/item")
        assert f"[URL:{host}]" in result, host
        assert "[URL:suspicious]" not in result


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
