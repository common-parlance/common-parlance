"""Configure Common Parlance to start automatically on login.

Platform-specific implementations:
  - macOS: launchd plist in ~/Library/LaunchAgents/ (via plistlib)
  - Linux: systemd user unit in ~/.config/systemd/user/ (hardened)
  - Windows: Task Scheduler task (avoids VBS/Startup folder AV flags)
"""

import logging
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

SERVICE_NAME = "com.common-parlance.proxy"
TASK_NAME = "CommonParlanceProxy"
DESCRIPTION = "Common Parlance — transparent AI conversation proxy"


def _find_executable() -> str:
    """Find the common-parlance executable path."""
    exe = shutil.which("common-parlance")
    if exe:
        return exe
    # Fallback: use the Python that's running us
    return f"{sys.executable} -m common_parlance.cli"


def _log_dir() -> Path:
    """Platform-appropriate log directory."""
    system = platform.system()
    if system == "Darwin":
        path = Path.home() / "Library" / "Logs" / "common-parlance"
    else:
        path = Path.home() / ".local" / "share" / "common-parlance" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _get_uid() -> int:
    """Get the current user's UID (macOS/Linux)."""
    return os.getuid()


# --- macOS (launchd) ---


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_NAME}.plist"


def _macos_install() -> Path:
    """Install a launchd plist for macOS.

    Uses plistlib for proper XML escaping. Adds NetworkState to
    KeepAlive since we're a network proxy.
    """
    exe = _find_executable()
    plist_path = _macos_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = str(_log_dir() / "proxy.log")

    plist_data = {
        "Label": SERVICE_NAME,
        "ProgramArguments": [*exe.split(), "proxy"],
        "RunAtLoad": True,
        "KeepAlive": {
            "SuccessfulExit": False,
            "NetworkState": True,
        },
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
        "ProcessType": "Background",
    }
    with open(plist_path, "wb") as f:
        plistlib.dump(plist_data, f)

    # launchd requires 644 on user plists
    os.chmod(str(plist_path), 0o644)
    logger.info("Wrote launchd plist to %s", plist_path)
    return plist_path


def _macos_load(plist_path: Path) -> bool:
    """Load (start) the launchd service using modern bootstrap API."""
    domain_target = f"gui/{_get_uid()}"
    # Bootout first in case it's already loaded (ignore errors)
    subprocess.run(
        ["launchctl", "bootout", domain_target, str(plist_path)],
        capture_output=True,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", domain_target, str(plist_path)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _macos_uninstall() -> bool:
    plist_path = _macos_plist_path()
    if plist_path.exists():
        domain_target = f"gui/{_get_uid()}"
        subprocess.run(
            ["launchctl", "bootout", domain_target, str(plist_path)],
            capture_output=True,
        )
        plist_path.unlink()
        return True
    return False


# --- Linux (systemd) ---


def _linux_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "common-parlance.service"


def _linux_install() -> Path:
    """Install a hardened systemd user unit for Linux.

    Includes security directives (NoNewPrivileges, ProtectSystem, etc.)
    and proper network dependency ordering.
    """
    exe = _find_executable()
    unit_path = _linux_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)

    # %h expands to the user's home directory in systemd
    unit_content = f"""\
[Unit]
Description={DESCRIPTION}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart="{exe}" proxy
Restart=on-failure
RestartSec=10

# Security hardening
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
ReadWritePaths=%h/.local/share/common-parlance
ReadWritePaths=%h/.config/common-parlance

[Install]
WantedBy=default.target
"""
    unit_path.write_text(unit_content)
    logger.info("Wrote systemd unit to %s", unit_path)
    return unit_path


def _linux_enable() -> bool:
    """Enable and start the systemd user unit."""
    # Reload in case the unit file changed
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True,
    )
    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", "common-parlance.service"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _linux_uninstall() -> bool:
    unit_path = _linux_unit_path()
    if unit_path.exists():
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "common-parlance.service"],
            capture_output=True,
        )
        unit_path.unlink()
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
        )
        return True
    return False


# --- Windows (Task Scheduler) ---


def _windows_install() -> bool:
    """Install a Task Scheduler task for Windows.

    Uses schtasks.exe — more robust than VBS/Startup folder and
    doesn't trigger AV false positives.
    """
    exe = _find_executable()
    parts = exe.split(maxsplit=1)
    program = parts[0]
    # Build arguments: remaining exe parts (if fallback) + "proxy"
    extra_args = parts[1] if len(parts) > 1 else ""
    arguments = f"{extra_args} proxy".strip()

    result = subprocess.run(
        [
            "schtasks",
            "/Create",
            "/TN",
            TASK_NAME,
            "/TR",
            f'"{program}" {arguments}',
            "/SC",
            "ONLOGON",
            "/RL",
            "LIMITED",
            "/F",  # force overwrite if exists
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("schtasks create failed: %s", result.stderr.strip())
        return False

    logger.info("Created scheduled task: %s", TASK_NAME)
    return True


def _windows_start() -> bool:
    """Start the scheduled task immediately."""
    result = subprocess.run(
        ["schtasks", "/Run", "/TN", TASK_NAME],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _windows_uninstall() -> bool:
    """Remove the scheduled task."""
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _windows_is_installed() -> bool:
    """Check if the scheduled task exists."""
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


# --- Public API ---


def install_autostart(*, start_now: bool = True) -> tuple[str, str]:
    """Install auto-start for the current platform.

    If start_now is True (default), also loads/enables the service immediately.
    Returns (platform_name, instructions) for the user.
    """
    system = platform.system()

    if system == "Darwin":
        path = _macos_install()
        log_path = _log_dir() / "proxy.log"
        started = _macos_load(path) if start_now else False
        status = "Loaded and running." if started else "Will start on next login."
        domain = f"gui/{_get_uid()}"
        return (
            "macOS (launchd)",
            f"Installed: {path}\n"
            f"Status:    {status}\n"
            f"Stop:      launchctl bootout {domain} '{path}'\n"
            f"Logs:      tail -f '{log_path}'",
        )
    elif system == "Linux":
        path = _linux_install()
        started = _linux_enable() if start_now else False
        status = "Enabled and running." if started else "Will start on next login."
        return (
            "Linux (systemd)",
            f"Installed: {path}\n"
            f"Status:    {status}\n"
            "Stop:      systemctl --user stop common-parlance\n"
            "Logs:      journalctl --user -u common-parlance -f",
        )
    elif system == "Windows":
        success = _windows_install()
        if not success:
            raise RuntimeError("Failed to create scheduled task")
        if start_now:
            _windows_start()
        started = start_now
        status = "Running." if started else "Will start on next login."
        return (
            "Windows (Task Scheduler)",
            f"Task:   {TASK_NAME}\n"
            f"Status: {status}\n"
            f"Stop:   schtasks /End /TN {TASK_NAME}\n"
            f"Remove: schtasks /Delete /TN {TASK_NAME} /F",
        )
    else:
        raise RuntimeError(f"Unsupported platform: {system}")


def uninstall_autostart() -> tuple[str, bool]:
    """Remove auto-start for the current platform.

    Returns (platform_name, was_installed).
    """
    system = platform.system()

    if system == "Darwin":
        return ("macOS (launchd)", _macos_uninstall())
    elif system == "Linux":
        return ("Linux (systemd)", _linux_uninstall())
    elif system == "Windows":
        was_installed = _windows_is_installed()
        if was_installed:
            _windows_uninstall()
        return ("Windows (Task Scheduler)", was_installed)
    else:
        raise RuntimeError(f"Unsupported platform: {system}")


def is_autostart_installed() -> bool:
    """Check if auto-start is currently installed."""
    system = platform.system()
    if system == "Darwin":
        return _macos_plist_path().exists()
    elif system == "Linux":
        return _linux_unit_path().exists()
    elif system == "Windows":
        return _windows_is_installed()
    return False
