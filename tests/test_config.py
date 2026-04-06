"""Tests for config load/save with file locking."""

import json
import os

from common_parlance.config import DEFAULT_CONFIG, load_config, save_config


def test_load_default_config(tmp_path, monkeypatch):
    """Loading with no config file returns defaults."""
    monkeypatch.setattr(
        "common_parlance.config.CONFIG_PATH", tmp_path / "nonexistent.json"
    )
    config = load_config()
    assert config == DEFAULT_CONFIG


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("common_parlance.config.CONFIG_PATH", config_path)

    custom = {"api_key": "test-key-123", "auto_approve": True}
    save_config(custom)

    loaded = load_config()
    assert loaded["api_key"] == "test-key-123"
    assert loaded["auto_approve"] is True
    # Defaults should still be present for unset keys
    assert loaded["port"] == DEFAULT_CONFIG["port"]


def test_save_creates_parent_dirs(tmp_path, monkeypatch):
    config_path = tmp_path / "nested" / "deep" / "config.json"
    monkeypatch.setattr("common_parlance.config.CONFIG_PATH", config_path)

    save_config({"api_key": "test"})
    assert config_path.exists()


def test_save_file_permissions(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("common_parlance.config.CONFIG_PATH", config_path)

    save_config({"api_key": "secret"})

    mode = os.stat(str(config_path)).st_mode & 0o777
    assert mode == 0o600, f"Expected 0600, got {oct(mode)}"


def test_load_corrupt_json_returns_defaults(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text("not valid json {{{")
    monkeypatch.setattr("common_parlance.config.CONFIG_PATH", config_path)

    config = load_config()
    assert config == DEFAULT_CONFIG


def test_user_config_overrides_defaults(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"port": 9999}))
    monkeypatch.setattr("common_parlance.config.CONFIG_PATH", config_path)

    config = load_config()
    assert config["port"] == 9999
    assert config["upstream"] == DEFAULT_CONFIG["upstream"]


def test_save_overwrites_existing(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("common_parlance.config.CONFIG_PATH", config_path)

    save_config({"api_key": "first"})
    save_config({"api_key": "second"})

    loaded = load_config()
    assert loaded["api_key"] == "second"
    # Note: second save only has api_key, so defaults fill in the rest
    assert "port" not in json.loads(config_path.read_text())
