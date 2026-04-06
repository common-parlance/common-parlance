"""Install a persistent import watcher as a system service.

Platform-specific implementations:
  - macOS: launchd plist with StartInterval in ~/Library/LaunchAgents/
  - Linux: systemd user timer + service in ~/.config/systemd/user/
  - Windows: Task Scheduler with repeat trigger
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

SERVICE_NAME = "com.common-parlance.watcher"
SYSTEMD_SERVICE = "common-parlance-watcher"
TASK_NAME = "CommonParlanceWatcher"


def _find_executable() -> str:
    exe = shutil.which("common-parlance")
    if exe:
        return exe
    return f"{sys.executable} -m common_parlance.cli"


def _log_dir() -> Path:
    system = platform.system()
    if system == "Darwin":
        path = Path.home() / "Library" / "Logs" / "common-parlance"
    else:
        path = Path.home() / ".local" / "share" / "common-parlance" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _get_uid() -> int:
    return os.getuid()


# --- macOS (launchd) ---


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_NAME}.plist"


def _macos_install(watch_path: str, interval_min: int, db_path: str) -> str:
    exe = _find_executable()
    plist_path = _macos_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = str(_log_dir() / "watcher.log")

    args = [*exe.split(), "import", watch_path, "--watch", str(interval_min)]
    if db_path:
        args.extend(["-d", db_path])

    plist_data = {
        "Label": SERVICE_NAME,
        "ProgramArguments": args,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
        "ProcessType": "Background",
    }
    with open(plist_path, "wb") as f:
        plistlib.dump(plist_data, f)
    os.chmod(str(plist_path), 0o644)

    # Load the service
    domain_target = f"gui/{_get_uid()}"
    subprocess.run(
        ["launchctl", "bootout", domain_target, str(plist_path)],
        capture_output=True,
    )
    subprocess.run(
        ["launchctl", "bootstrap", domain_target, str(plist_path)],
        capture_output=True,
    )

    return (
        f"Installed: {plist_path}\n"
        f"Logs:      tail -f '{log_path}'\n"
        f"Stop:      launchctl bootout {domain_target} '{plist_path}'\n"
        f"Remove:    rm '{plist_path}'"
    )


# --- Linux (systemd) ---


def _linux_install(watch_path: str, interval_min: int, db_path: str) -> str:
    exe = _find_executable()
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)

    args = f'"{exe}" import "{watch_path}" --watch {interval_min}'
    if db_path:
        args += f' -d "{db_path}"'

    service_content = f"""\
[Unit]
Description=Common Parlance import watcher
After=network-online.target

[Service]
Type=simple
ExecStart={args}
Restart=on-failure
RestartSec=30

NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/.local/share/common-parlance
ReadWritePaths=%h/.config/common-parlance

[Install]
WantedBy=default.target
"""
    service_path = unit_dir / f"{SYSTEMD_SERVICE}.service"
    service_path.write_text(service_content)

    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", f"{SYSTEMD_SERVICE}.service"],
        capture_output=True,
    )

    return (
        f"Installed: {service_path}\n"
        f"Status:    systemctl --user status {SYSTEMD_SERVICE}\n"
        f"Logs:      journalctl --user -u {SYSTEMD_SERVICE} -f\n"
        f"Stop:      systemctl --user stop {SYSTEMD_SERVICE}\n"
        f"Remove:    systemctl --user disable {SYSTEMD_SERVICE} && rm '{service_path}'"
    )


# --- Windows (Task Scheduler) ---


def _windows_install(watch_path: str, interval_min: int, db_path: str) -> str:
    exe = _find_executable()
    parts = exe.split(maxsplit=1)
    program = parts[0]
    extra = parts[1] if len(parts) > 1 else ""
    arguments = f'{extra} import "{watch_path}" --watch {interval_min}'.strip()
    if db_path:
        arguments += f' -d "{db_path}"'

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
            "/F",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create scheduled task: {result.stderr}")

    subprocess.run(
        ["schtasks", "/Run", "/TN", TASK_NAME],
        capture_output=True,
    )

    return (
        f"Task:   {TASK_NAME}\n"
        f"Stop:   schtasks /End /TN {TASK_NAME}\n"
        f"Remove: schtasks /Delete /TN {TASK_NAME} /F"
    )


# --- Public API ---


def install_watcher(
    watch_path: str, interval_min: int, db_path: str
) -> tuple[str, str]:
    """Install a persistent import watcher for the current platform.

    Returns (platform_name, info_string).
    """
    system = platform.system()

    if system == "Darwin":
        info = _macos_install(watch_path, interval_min, db_path)
        return "macOS (launchd)", info
    elif system == "Linux":
        info = _linux_install(watch_path, interval_min, db_path)
        return "Linux (systemd)", info
    elif system == "Windows":
        info = _windows_install(watch_path, interval_min, db_path)
        return "Windows (Task Scheduler)", info
    else:
        raise RuntimeError(f"Unsupported platform: {system}")


def uninstall_watcher() -> tuple[str, bool]:
    """Remove the import watcher service.

    Returns (platform_name, was_installed).
    """
    system = platform.system()

    if system == "Darwin":
        plist_path = _macos_plist_path()
        if plist_path.exists():
            domain = f"gui/{_get_uid()}"
            subprocess.run(
                ["launchctl", "bootout", domain, str(plist_path)],
                capture_output=True,
            )
            plist_path.unlink()
            return "macOS (launchd)", True
        return "macOS (launchd)", False

    elif system == "Linux":
        unit = (
            Path.home() / ".config" / "systemd" / "user" / f"{SYSTEMD_SERVICE}.service"
        )
        if unit.exists():
            subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "disable",
                    "--now",
                    f"{SYSTEMD_SERVICE}.service",
                ],
                capture_output=True,
            )
            unit.unlink()
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"], capture_output=True
            )
            return "Linux (systemd)", True
        return "Linux (systemd)", False

    elif system == "Windows":
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME],
            capture_output=True,
        )
        if result.returncode == 0:
            subprocess.run(
                ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                capture_output=True,
            )
            return "Windows (Task Scheduler)", True
        return "Windows (Task Scheduler)", False

    else:
        raise RuntimeError(f"Unsupported platform: {system}")
