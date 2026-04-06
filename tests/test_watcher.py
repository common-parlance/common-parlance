"""Tests for watcher utility functions."""

import sys
from unittest.mock import patch

from common_parlance.watcher import (
    SERVICE_NAME,
    SYSTEMD_SERVICE,
    TASK_NAME,
    _find_executable,
)


def test_service_name_constants():
    assert SERVICE_NAME == "com.common-parlance.watcher"
    assert SYSTEMD_SERVICE == "common-parlance-watcher"
    assert TASK_NAME == "CommonParlanceWatcher"


def test_find_executable_uses_which():
    with patch("shutil.which", return_value="/usr/local/bin/common-parlance"):
        result = _find_executable()
    assert result == "/usr/local/bin/common-parlance"


def test_find_executable_falls_back_to_sys_executable():
    with patch("shutil.which", return_value=None):
        result = _find_executable()
    assert sys.executable in result
    assert "common_parlance.cli" in result
