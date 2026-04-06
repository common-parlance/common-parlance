"""Tests for consent flow."""

import sys

from common_parlance.consent import (
    check_consent_interactive,
    grant_consent,
    has_consent,
    revoke_consent,
)


def test_has_consent_true(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("common_parlance.config.CONFIG_PATH", config_path)

    grant_consent()
    assert has_consent() is True


def test_has_consent_false_when_revoked(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("common_parlance.config.CONFIG_PATH", config_path)

    grant_consent()
    revoke_consent()
    assert has_consent() is False


def test_has_consent_false_when_never_set(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("common_parlance.config.CONFIG_PATH", config_path)

    assert has_consent() is False


def test_grant_consent_stores_timestamp(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("common_parlance.config.CONFIG_PATH", config_path)

    grant_consent()

    from common_parlance.config import load_config

    config = load_config()
    assert config["consent"] is True
    assert "consent_timestamp" in config
    # ISO 8601 timestamp should contain a T
    assert "T" in config["consent_timestamp"]


def test_revoke_consent_clears_consent(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("common_parlance.config.CONFIG_PATH", config_path)

    grant_consent()
    revoke_consent()

    from common_parlance.config import load_config

    config = load_config()
    assert config["consent"] is False


def test_grant_revoke_roundtrip(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("common_parlance.config.CONFIG_PATH", config_path)

    assert has_consent() is False
    grant_consent()
    assert has_consent() is True
    revoke_consent()
    assert has_consent() is False
    grant_consent()
    assert has_consent() is True


def test_check_interactive_returns_cached_true(tmp_path, monkeypatch):
    """If consent already granted, returns True without prompting."""
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("common_parlance.config.CONFIG_PATH", config_path)

    grant_consent()
    assert check_consent_interactive() is True


def test_check_interactive_returns_cached_false(tmp_path, monkeypatch):
    """If consent already revoked, returns False without prompting."""
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("common_parlance.config.CONFIG_PATH", config_path)

    revoke_consent()
    assert check_consent_interactive() is False


def test_check_interactive_non_tty_returns_false(tmp_path, monkeypatch):
    """Non-TTY environment returns False without prompting."""
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("common_parlance.config.CONFIG_PATH", config_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert check_consent_interactive() is False
