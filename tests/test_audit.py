"""Tests for PII audit scanning and density checks."""

import json

from common_parlance.audit import (
    AuditResult,
    ConversationAudit,
    audit_conversations,
)

OK = {"role": "assistant", "content": "OK"}


def _row(conv_id, turns):
    """Helper to create a fake row tuple."""
    return (conv_id, json.dumps(turns))


# --- Leak detection ---


def test_no_leaks_clean_text():
    rows = [
        _row(
            "abc12345",
            [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"},
            ],
        )
    ]
    result = audit_conversations(rows)
    assert result.leak_count == 0
    assert result.total == 1
    assert not result.has_leaks


def test_detects_email_leak():
    rows = [
        _row(
            "abc12345",
            [
                {"role": "user", "content": "Contact alice@example.com"},
                OK,
            ],
        )
    ]
    result = audit_conversations(rows)
    assert result.leak_count == 1
    assert result.has_leaks
    assert "email" in result.conversations[0].leaks


def test_detects_phone_leak():
    rows = [
        _row(
            "abc12345",
            [
                {"role": "user", "content": "Call me at 555-123-4567"},
                OK,
            ],
        )
    ]
    result = audit_conversations(rows)
    assert result.leak_count == 1
    assert "phone" in result.conversations[0].leaks


def test_detects_ssn_leak():
    rows = [
        _row(
            "abc12345",
            [
                {"role": "user", "content": "My SSN is 123-45-6789"},
                OK,
            ],
        )
    ]
    result = audit_conversations(rows)
    assert "ssn" in result.conversations[0].leaks


def test_detects_api_key_leak():
    key = "sk-abcdefghijklmnopqrstuvwxyz"
    rows = [
        _row(
            "abc12345",
            [
                {"role": "user", "content": f"My key is {key}"},
                OK,
            ],
        )
    ]
    result = audit_conversations(rows)
    assert "api_key" in result.conversations[0].leaks


def test_detects_file_path_leak():
    rows = [
        _row(
            "abc12345",
            [
                {"role": "user", "content": "Check /Users/john/secrets.txt"},
                OK,
            ],
        )
    ]
    result = audit_conversations(rows)
    assert "file_path" in result.conversations[0].leaks


def test_ignores_benign_ip_addresses():
    """IPs starting with 0., 1.0, 2.0, 127. are filtered out."""
    rows = [
        _row(
            "abc12345",
            [
                {"role": "user", "content": "Use 127.0.0.1 or 0.0.0.0"},
                OK,
            ],
        )
    ]
    result = audit_conversations(rows)
    assert "ip_address" not in result.conversations[0].leaks


def test_detects_real_ip_leak():
    rows = [
        _row(
            "abc12345",
            [
                {"role": "user", "content": "Server is at 192.168.1.100"},
                OK,
            ],
        )
    ]
    result = audit_conversations(rows)
    assert "ip_address" in result.conversations[0].leaks


# --- Density ---


def test_high_density_flagged():
    # 4 words, 2 placeholders = 50% density > 25% threshold
    rows = [
        _row(
            "abc12345",
            [
                {"role": "user", "content": "Hello [NAME_1] and [EMAIL]"},
                OK,
            ],
        )
    ]
    result = audit_conversations(rows, density_threshold=0.25)
    assert result.high_density_count == 1


def test_low_density_not_flagged():
    rows = [
        _row(
            "abc12345",
            [
                {
                    "role": "user",
                    "content": "This is a normal conversation about Python",
                },
                {
                    "role": "assistant",
                    "content": "Python is great for scripting",
                },
            ],
        )
    ]
    result = audit_conversations(rows, density_threshold=0.25)
    assert result.high_density_count == 0


# --- Multiple conversations ---


def test_multiple_conversations():
    rows = [
        _row(
            "conv1",
            [
                {"role": "user", "content": "Clean text"},
                OK,
            ],
        ),
        _row(
            "conv2",
            [
                {"role": "user", "content": "Email: bob@test.com"},
                OK,
            ],
        ),
        _row(
            "conv3",
            [
                {"role": "user", "content": "Also clean"},
                {"role": "assistant", "content": "Sure"},
            ],
        ),
    ]
    result = audit_conversations(rows)
    assert result.total == 3
    assert result.leak_count == 1


# --- Dataclass properties ---


def test_audit_result_properties():
    result = AuditResult(total=0, conversations=[], leak_count=0)
    assert not result.has_leaks

    result = AuditResult(total=1, conversations=[], leak_count=1)
    assert result.has_leaks


def test_conversation_audit_fields():
    c = ConversationAudit(
        conv_id="abc12345",
        turn_count=2,
        word_count=10,
        placeholder_count=1,
        density=0.1,
        preview="Hello world",
    )
    assert c.conv_id == "abc12345"
    assert c.leaks == {}
