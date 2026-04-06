"""User configuration for Common Parlance."""

import json
import logging
import os
import stat
import sys
from pathlib import Path

if sys.platform != "win32":
    import fcntl

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "upstream": "http://localhost:11434",
    "port": 11435,
    "database": str(
        Path.home() / ".local" / "share" / "common-parlance" / "conversations.db"
    ),
    "auto_approve": False,
    "use_presidio": True,
    "proxy_url": "https://common-parlance-proxy.common-parlance.workers.dev",
    "api_key": "",
    "upload_interval_hours": 24,
}

CONFIG_PATH = Path(
    os.environ.get("COMMON_PARLANCE_CONFIG", "")
    or str(Path.home() / ".config" / "common-parlance" / "config.json")
)


def load_config() -> dict:
    """Load config from disk, falling back to defaults.

    Uses a shared lock (LOCK_SH) to prevent reading while another
    process is writing. The lock is released when the file is closed.
    """
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                if sys.platform != "win32":
                    fcntl.flock(f, fcntl.LOCK_SH)
                user_config = json.load(f)
            config.update(user_config)
            logger.info("Loaded config from %s", CONFIG_PATH)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load config: %s", e)
    return config


def save_config(config: dict) -> None:
    """Save config to disk with restricted permissions (contains API key).

    Uses an exclusive lock (LOCK_EX) to prevent concurrent reads/writes.
    The lock is released when the file is closed.
    """
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("Cannot create config directory: %s", e)
        raise

    # Write with owner-only permissions (0600) since file contains API key
    try:
        fd = os.open(
            str(CONFIG_PATH),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(fd, "w") as f:
            if sys.platform != "win32":
                fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(config, f, indent=2)
        logger.info("Saved config to %s", CONFIG_PATH)
    except OSError as e:
        logger.error("Cannot save config: %s", e)
        raise
