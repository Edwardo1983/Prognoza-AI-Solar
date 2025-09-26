"""Utilities for managing an OpenVPN tunnel on demand."""
from __future__ import annotations

import os
import platform
import socket
import subprocess
import time
from pathlib import Path
from typing import Dict, Iterable, Optional

import psutil
import shutil

from app import settings

OPENVPN_HOST = settings.VPN_HEALTH_HOST
OPENVPN_PORT = settings.VPN_HEALTH_PORT

PREFERRED_METHOD_ENV = "OPENVPN_PREFERRED_METHOD"
CLI_OVERRIDE_ENV = "OPENVPN_CLI_PATH"
GUI_OVERRIDE_ENV = "OPENVPN_GUI_PATH"


def _first_existing_path(paths: Iterable[Optional[Path]]) -> Optional[Path]:
    """Return the first existing executable path from an iterable."""
    seen: set[Path] = set()
    for candidate in paths:
        if not candidate:
            continue
        path = candidate.expanduser()
        if not path.exists() or not path.is_file():
            continue
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        if os.name != "nt" and not os.access(resolved, os.X_OK):
            continue
        return resolved
    return None


def _find_openvpn_gui() -> Optional[Path]:
    """Attempt to locate the OpenVPN GUI executable on Windows systems."""
    override = os.environ.get(GUI_OVERRIDE_ENV)
    if override:
        override_path = Path(override).expanduser()
        if override_path.is_file():
            return override_path.resolve()

    candidates: list[Path] = []
    if platform.system().lower() == "windows":
        env_keys = ["ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"]
        for key in env_keys:
            base = os.environ.get(key)
            if base:
                candidates.append(Path(base) / "OpenVPN" / "bin" / "openvpn-gui.exe")

    for path_entry in os.environ.get("PATH", "").split(os.pathsep):
        if path_entry:
            base_path = Path(path_entry)
            candidates.append(base_path / "openvpn-gui.exe")
            candidates.append(base_path / "OpenVPN-GUI.exe")

    return _first_existing_path(candidates)


def _find_openvpn_cli(gui_hint: Optional[Path] = None) -> Optional[Path]:
    """Locate the OpenVPN CLI binary."""
    override = os.environ.get(CLI_OVERRIDE_ENV)
    exe_name = "openvpn.exe" if os.name == "nt" else "openvpn"

    candidates: list[Path] = []
    if override:
        candidates.append(Path(override).expanduser())

    which = shutil.which("openvpn")
    if which:
        candidates.append(Path(which))

    if gui_hint:
        candidates.append(gui_hint.parent / exe_name)

    if os.name == "nt":
        env_keys = ["ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "ProgramData", "USERPROFILE"]
        for key in env_keys:
            base = os.environ.get(key)
            if base:
                candidates.append(Path(base) / "OpenVPN" / "bin" / exe_name)
    else:
        candidates.extend(
            Path(path)
            for path in (
                "/usr/sbin/openvpn",
                "/usr/local/sbin/openvpn",
                "/usr/local/bin/openvpn",
                "/opt/homebrew/sbin/openvpn",
                "/opt/homebrew/bin/openvpn",
            )
        )

    return _first_existing_path(candidates)


def _gui_config_dirs() -> list[Path]:
    """Return known directories where OpenVPN GUI looks for profiles."""
    if platform.system().lower() != "windows":
        return []

    bases: list[Path] = []
    for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "ProgramData", "USERPROFILE"):
        base = os.environ.get(key)
        if base:
            bases.append(Path(base))
    bases.append(Path.home())

    directories: list[Path] = []
    for base in bases:
        for suffix in ("config", "config-auto"):
            candidate = base / "OpenVPN" / suffix
            if candidate not in directories:
                directories.append(candidate)
    return directories


def _gui_profile_exists(profile: str, ovpn_path: Path) -> bool:
    """Return True if the profile is available to the OpenVPN GUI."""
    expected_name = f"{profile}.ovpn"
    try:
        resolved_file = ovpn_path.resolve()
    except OSError:
        resolved_file = ovpn_path

    for directory in _gui_config_dirs():
        candidate = directory / expected_name
        if candidate.exists():
            return True
        try:
            if resolved_file.is_relative_to(directory):
                return True
        except AttributeError:
            try:
                resolved_file.relative_to(directory)
            except ValueError:
                pass
            else:
                return True
        except ValueError:
            continue
    return False


def _choose_method(
    detection: Dict[str, Optional[Path]], profile: str, ovpn_file: Path
) -> tuple[str, Optional[str]]:
    """Pick the most reliable method to control OpenVPN."""
    method = detection["method"]
    gui_path = detection.get("gui_path")
    cli_path = detection.get("cli_path")

    if method == "gui":
        if not gui_path:
            return "missing", "OpenVPN GUI executable not found. Install OpenVPN GUI."
        if not _gui_profile_exists(profile, ovpn_file):
            if cli_path:
                return "cli", None
            hint_dirs = [str(path) for path in _gui_config_dirs()]
            hint_text = ", ".join(hint_dirs) if hint_dirs else r"%ProgramFiles%\OpenVPN\config"
            return (
                "missing",
                (
                    f"OpenVPN GUI profile '{profile}' not found. Move {ovpn_file.name} into one of: {hint_text}, "
                    "or expose the CLI binary by adding it to PATH."
                ),
            )

    if method == "cli":
        if not cli_path:
            return "missing", "OpenVPN CLI executable not found. Install OpenVPN or add it to PATH."
        return "cli", None

    if method == "missing":
        return "missing", "OpenVPN executable not found. Install OpenVPN GUI or CLI."

    return method, None


def find_openvpn() -> Dict[str, Optional[Path]]:
    """Detect available OpenVPN entry points."""
    gui_path = _find_openvpn_gui()
    cli_path = _find_openvpn_cli(gui_path)
    preferred = os.environ.get(PREFERRED_METHOD_ENV, "").strip().lower()

    if preferred == "gui" and gui_path:
        method = "gui"
    elif preferred == "cli" and cli_path:
        method = "cli"
    elif cli_path:
        method = "cli"
    elif gui_path:
        method = "gui"
    else:
        method = "missing"

    return {"method": method, "gui_path": gui_path, "cli_path": cli_path}


def _profile_name(ovpn_path: Path) -> str:
    return ovpn_path.stem


def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    """Check whether a TCP connection can be established."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _is_process_alive(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def _find_openvpn_process_for_profile(profile: str) -> Optional[psutil.Process]:
    profile_lower = profile.lower()
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "openvpn" in name or "openvpn" in cmdline:
            if profile_lower in cmdline or profile_lower in name:
                return proc
    return None


def _write_pid_file(pid: int) -> None:
    settings.VPN_PID_FILE.write_text(str(pid), encoding="utf-8")


def _read_pid_file() -> Optional[int]:
    if not settings.VPN_PID_FILE.exists():
        return None
    try:
        return int(settings.VPN_PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _remove_pid_file() -> None:
    if settings.VPN_PID_FILE.exists():
        try:
            settings.VPN_PID_FILE.unlink()
        except OSError:
            pass


def _spawn_openvpn_cli(ovpn_path: Path, cli_path: Path) -> Optional[int]:
    """Launch the OpenVPN CLI in a detached subprocess."""
    creationflags = 0
    preexec_fn = None
    if os.name == "nt":  # pragma: win32-no-cover
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    else:
        preexec_fn = os.setsid  # type: ignore[attr-defined]

    with settings.VPN_LOG_FILE.open("ab") as log_handle:
        process = subprocess.Popen(  # noqa: S603,S607
            [str(cli_path), "--config", str(ovpn_path)],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            preexec_fn=preexec_fn,
            close_fds=True,
        )
    _write_pid_file(process.pid)
    return process.pid


def start_vpn(ovpn_path: str, start_timeout_s: int = 25) -> Dict[str, object]:
    """Start an OpenVPN tunnel if possible."""
    ovpn_file = Path(ovpn_path)
    detection = find_openvpn()
    profile = _profile_name(ovpn_file)

    if not ovpn_file.exists():
        return {
            "running": False,
            "method": detection["method"],
            "pid": None,
            "message": f"Configuration file not found: {ovpn_file}",
        }

    selected_method, error_message = _choose_method(detection, profile, ovpn_file)
    if selected_method == "missing":
        return {
            "running": False,
            "method": detection["method"],
            "pid": None,
            "message": error_message
            or "OpenVPN executable not found. Install OpenVPN GUI or CLI.",
        }

    start_time = time.time()
    pid: Optional[int] = None

    if selected_method == "gui" and detection["gui_path"]:
        command = [str(detection["gui_path"]), "--command", "connect", profile]
        subprocess.run(command, check=False)  # noqa: S603
    elif selected_method == "cli" and detection["cli_path"]:
        existing_pid = _read_pid_file()
        if existing_pid and _is_process_alive(existing_pid):
            pid = existing_pid
        else:
            pid = _spawn_openvpn_cli(ovpn_file, detection["cli_path"])
    else:
        return {
            "running": False,
            "method": detection["method"],
            "pid": None,
            "message": "Unable to determine how to start OpenVPN.",
        }

    running = False
    while time.time() - start_time < start_timeout_s:
        time.sleep(1)
        if selected_method == "gui":
            process = _find_openvpn_process_for_profile(profile)
            running = process is not None
            if running:
                pid = process.pid
        else:
            pid = pid or _read_pid_file()
            running = bool(pid and _is_process_alive(pid))

        if running and _tcp_reachable(OPENVPN_HOST, OPENVPN_PORT):
            break
    else:
        running = running and _tcp_reachable(OPENVPN_HOST, OPENVPN_PORT)

    message = "VPN started" if running else "VPN failed to start or is unreachable"
    return {"running": running, "method": selected_method, "pid": pid, "message": message}


def stop_vpn(ovpn_path: str) -> Dict[str, object]:
    """Stop the OpenVPN tunnel if it is running."""
    ovpn_file = Path(ovpn_path)
    detection = find_openvpn()
    profile = _profile_name(ovpn_file)

    method, error_message = _choose_method(detection, profile, ovpn_file)
    if method == "missing":
        return {
            "running": False,
            "method": detection["method"],
            "pid": None,
            "message": error_message or "OpenVPN executable not found.",
        }

    if method == "gui" and detection["gui_path"]:
        command = [str(detection["gui_path"]), "--command", "disconnect", profile]
        subprocess.run(command, check=False)  # noqa: S603
        time.sleep(2)
        process = _find_openvpn_process_for_profile(profile)
        running = process is not None
        pid = process.pid if process else None
        message = "Disconnect command issued via GUI"
    elif method == "cli":
        pid = _read_pid_file()
        if not pid:
            return {
                "running": False,
                "method": "cli",
                "pid": None,
                "message": "No PID recorded; VPN CLI does not appear to be running.",
            }
        try:
            process = psutil.Process(pid)
            process.terminate()
            try:
                process.wait(timeout=10)
            except psutil.TimeoutExpired:
                process.kill()
        except psutil.NoSuchProcess:
            pass
        _remove_pid_file()
        running = False
        message = "VPN CLI process terminated"
        pid = None
    else:
        return {
            "running": False,
            "method": detection["method"],
            "pid": None,
            "message": "OpenVPN executable not found.",
        }

    return {"running": running, "method": method, "pid": pid, "message": message}


def vpn_status(ovpn_path: str) -> Dict[str, object]:
    """Return the current status of the VPN connection."""
    ovpn_file = Path(ovpn_path)
    detection = find_openvpn()
    profile = _profile_name(ovpn_file)

    method, error_message = _choose_method(detection, profile, ovpn_file)
    if method == "missing":
        return {
            "running": False,
            "method": detection["method"],
            "pid": None,
            "message": error_message or "OpenVPN executable not found.",
        }

    pid: Optional[int] = None
    running = False

    if method == "gui":
        process = _find_openvpn_process_for_profile(profile)
        running = process is not None
        pid = process.pid if process else None
    elif method == "cli":
        pid = _read_pid_file()
        running = bool(pid and _is_process_alive(pid))
    else:
        running = False

    reachable = _tcp_reachable(OPENVPN_HOST, OPENVPN_PORT) if running else False
    message = "VPN reachable" if reachable else "VPN not connected"

    return {
        "running": running and reachable,
        "method": method,
        "pid": pid,
        "message": message,
    }
