"""Utilities for managing an OpenVPN tunnel on demand."""
from __future__ import annotations

import os
import platform
import re
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import psutil
import shutil

from app import settings

OPENVPN_HOST = settings.VPN_HEALTH_HOST
OPENVPN_PORT = settings.VPN_HEALTH_PORT

PREFERRED_METHOD_ENV = "OPENVPN_PREFERRED_METHOD"
CLI_OVERRIDE_ENV = "OPENVPN_CLI_PATH"
GUI_OVERRIDE_ENV = "OPENVPN_GUI_PATH"

_LOG_HINTS: List[Tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"Access is denied", re.IGNORECASE),
        (
            "Windows blocked TAP driver or routing changes (Access is denied). "
            "Run the command shell as Administrator or install the OpenVPN service."
        ),
    ),
    (
        re.compile(r"AUTH_FAILED", re.IGNORECASE),
        "Authentication failed. Verify the VPN credentials or certificate permissions.",
    ),
    (
        re.compile(r"certificate verify failed", re.IGNORECASE),
        "Certificate validation failed. Check CA and client certificate files.",
    ),
]


def _normalize_profile_name(name: str) -> str:
    """Normalize profile names for fuzzy matching."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _resolve_ovpn_file(ovpn_path: str) -> Tuple[Optional[Path], List[str], Optional[str]]:
    """Resolve a user-supplied path to an existing .ovpn file."""
    raw_path = Path(ovpn_path).expanduser()
    candidates: List[Path] = []
    attempted: List[str] = []

    def _register_candidate(path: Path) -> None:
        candidate = path.expanduser()
        try:
            candidate = candidate.resolve(strict=False)
        except OSError:
            pass
        if candidate not in candidates:
            candidates.append(candidate)
            attempted.append(str(candidate))

    _register_candidate(raw_path)
    if raw_path.suffix.lower() != ".ovpn":
        _register_candidate(raw_path.with_suffix(".ovpn"))
    if not raw_path.is_absolute():
        base_candidate = settings.BASE_DIR / raw_path
        _register_candidate(base_candidate)
        if raw_path.suffix.lower() != ".ovpn":
            _register_candidate((settings.BASE_DIR / raw_path).with_suffix(".ovpn"))

    for candidate in candidates:
        if candidate.exists():
            return candidate, attempted, None

    target_name = raw_path.name if raw_path.name not in {"", "."} else ""
    normalized_target = _normalize_profile_name(Path(target_name).stem or target_name)

    if normalized_target:
        search_dirs = {candidate.parent for candidate in candidates if candidate.parent}
        search_dirs.add(settings.SECRETS_DIR)
        for directory in list(search_dirs):
            if not directory.exists():
                continue
            for file in directory.glob("*.ovpn"):
                if _normalize_profile_name(file.stem) == normalized_target:
                    try:
                        resolved_file = file.resolve(strict=False)
                    except OSError:
                        resolved_file = file
                    return (
                        resolved_file,
                        attempted,
                        f"Resolved '{ovpn_path}' to '{resolved_file}'",
                    )

    default_file = settings.DEFAULT_OVPN_PATH
    if not ovpn_path and default_file.exists():
        try:
            resolved_default = default_file.resolve(strict=False)
        except OSError:
            resolved_default = default_file
        return (
            resolved_default,
            attempted,
            f"Using default configuration at '{resolved_default}'",
        )

    return None, attempted, None


def _read_log_tail(max_bytes: int = 8192, max_lines: int = 120) -> List[str]:
    """Return the last portion of the OpenVPN log file."""
    path = settings.VPN_LOG_FILE
    if not path.exists():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes), os.SEEK_SET)
            data = handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return []
    lines = data.splitlines()
    if len(lines) > max_lines:
        return lines[-max_lines:]
    return lines


def _diagnose_start_failure() -> Optional[str]:
    """Inspect the log tail for known failure signatures."""
    lines = _read_log_tail()
    if not lines:
        return None
    joined = "\n".join(lines)
    for pattern, message in _LOG_HINTS:
        if pattern.search(joined):
            return message
    for line in reversed(lines):
        if "error" in line.lower():
            return f"Latest OpenVPN log error: {line}"
    return None


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


@dataclass
class _CliState:
    """Snapshot of the CLI runtime state and recent log analysis."""

    pid: Optional[int]
    process_running: bool
    connected: bool
    reachable: bool
    last_error: Optional[str]
    last_log_line: Optional[str]


def _find_openvpn_gui() -> Optional[Path]:
    """Attempt to locate the OpenVPN GUI executable on Windows systems."""
    override = os.environ.get(GUI_OVERRIDE_ENV)
    if override:
        override_path = Path(override).expanduser()
        if override_path.is_file():
            return override_path.resolve()

    candidates: List[Path] = []
    if platform.system().lower() == "windows":
        env_keys = ["ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"]
        for key in env_keys:
            base = os.environ.get(key)
            if base:
                candidates.append(Path(base) / "OpenVPN" / "bin" / "openvpn-gui.exe")

    for path_entry in os.environ.get("PATH", "").split(os.pathsep):
        if path_entry:
            candidates.append(Path(path_entry) / "OpenVPNGUI.exe")

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _find_openvpn_cli(gui_hint: Optional[Path] = None) -> Optional[Path]:
    """Locate the OpenVPN CLI binary."""
    override = os.environ.get(CLI_OVERRIDE_ENV)
    exe_name = "openvpn.exe" if os.name == "nt" else "openvpn"

    candidates: List[Path] = []
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


def _gui_config_dirs() -> List[Path]:
    """Return known directories where OpenVPN GUI looks for profiles."""
    if platform.system().lower() != "windows":
        return []

    bases: List[Path] = []
    for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "ProgramData", "USERPROFILE"):
        base = os.environ.get(key)
        if base:
            bases.append(Path(base))
    bases.append(Path.home())

    directories: List[Path] = []
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
) -> Tuple[str, Optional[str]]:
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
    """Detect available OpenVPN entry points.

    Returns
    -------
    dict
        Dictionary with keys ``method`` (``"gui"``, ``"cli"``, or ``"missing"``),
        ``gui_path``, and ``cli_path`` representing resolved executable paths.
    """
    gui_path: Optional[Path] = None
    cli_path: Optional[Path] = None

    if platform.system().lower() == "windows":
        gui_path = _find_openvpn_gui()
    cli_path = _find_openvpn_cli()

    if gui_path:
        method = "gui"
    elif cli_path:
        method = "cli"

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


def _read_log_tail(max_bytes: int = LOG_TAIL_BYTES) -> list[str]:
    """Return the tail of the OpenVPN log to avoid loading large files."""

    log_path = settings.VPN_LOG_FILE
    if not log_path.exists():
        return []

    try:
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size <= max_bytes:
                handle.seek(0)
            else:
                handle.seek(-max_bytes, os.SEEK_END)
            data = handle.read()
    except OSError:
        return []

    return data.decode("utf-8", errors="ignore").splitlines()


def _interpret_log(lines: list[str]) -> tuple[bool, Optional[str], Optional[str]]:
    """Inspect log lines looking for success or well-known failure messages."""

    if not lines:
        return False, None, None

    failure_patterns = [
        ("auth_failed", "Authentication failed"),
        ("cannot open tun/tap", "Cannot open tunnel interface"),
        ("permission denied", "Permission denied while configuring tunnel"),
        ("exiting due to fatal error", "OpenVPN exited due to a fatal error"),
        ("connection reset", "Connection reset by peer"),
        ("tls error", "TLS handshake error"),
        ("sigterm", "Session terminated"),
    ]

    last_line: Optional[str] = None
    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line:
            continue
        last_line = line if last_line is None else last_line
        lowered = line.lower()
        if "initialization sequence completed" in lowered:
            return True, None, last_line
        for pattern, message in failure_patterns:
            if pattern in lowered:
                return False, f"{message}: {line}", last_line

    return False, None, last_line


def _build_cli_state(expected_pid: Optional[int] = None) -> _CliState:
    """Gather runtime state for the CLI based tunnel."""

    pid = expected_pid or _read_pid_file()
    process_running = bool(pid and _is_process_alive(pid))
    if not process_running:
        pid = None
        _remove_pid_file()

    connected_from_log, failure_reason, last_log_line = _interpret_log(_read_log_tail())
    connected = process_running and connected_from_log
    reachable = connected and _tcp_reachable(OPENVPN_HOST, OPENVPN_PORT)

    return _CliState(
        pid=pid,
        process_running=process_running,
        connected=connected,
        reachable=reachable,
        last_error=failure_reason,
        last_log_line=last_log_line,
    )



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

    settings.VPN_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
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
    resolved_path, attempted_paths, resolution_note = _resolve_ovpn_file(ovpn_path)
    detection = find_openvpn()
    display_input = str(Path(ovpn_path)) if ovpn_path else "<default>"
    profile_source = resolved_path or Path(ovpn_path or settings.DEFAULT_OVPN_PATH)
    profile = _profile_name(profile_source)

    if not resolved_path:
        hint = ""
        if attempted_paths:
            hint = f" Checked: {', '.join(attempted_paths)}."
        return {
            "running": False,
            "reachable": False,
            "method": detection["method"],
            "pid": None,
            "message": f"Configuration file not found: {display_input}{hint}",
        }

    selected_method, error_message = _choose_method(detection, profile, resolved_path)
    if selected_method == "missing":
        return {
            "running": False,
            "reachable": False,
            "method": "missing",
            "pid": None,
            "message": "OpenVPN executable not found. Install OpenVPN GUI or CLI.",
        }

    deadline = time.time() + start_timeout_s

    if detection["method"] == "gui" and detection["gui_path"]:
        command = [str(detection["gui_path"]), "--command", "connect", profile]
        subprocess.run(command, check=False)  # noqa: S603

        while time.time() < deadline:
            time.sleep(1)
            process = _find_openvpn_process_for_profile(profile)
            pid = process.pid if process else None
            running = process is not None
            reachable = running and _tcp_reachable(OPENVPN_HOST, OPENVPN_PORT)
            if running and reachable:
                return {
                    "running": True,
                    "reachable": True,
                    "method": "gui",
                    "pid": pid,
                    "message": "VPN connected via GUI.",
                }

        process = _find_openvpn_process_for_profile(profile)
        pid = process.pid if process else None
        running = process is not None
        reachable = running and _tcp_reachable(OPENVPN_HOST, OPENVPN_PORT)
        if running and not reachable:
            message = (
                "OpenVPN GUI session detected but VPN health check host"
                f" {OPENVPN_HOST}:{OPENVPN_PORT} is unreachable."
            )
        else:
            pid = _spawn_openvpn_cli(resolved_path, detection["cli_path"])
    else:
        return {
            "running": False,
            "reachable": state.reachable,
            "method": "cli",
            "pid": state.pid,
            "message": failure_message,
        }

    return {
        "running": False,
        "reachable": False,
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

    base_message = "VPN started" if running else "VPN failed to start or is unreachable"
    if resolution_note:
        base_message = f"{base_message} ({resolution_note})"
    if not running:
        failure_hint = _diagnose_start_failure()
        if failure_hint:
            base_message = f"{base_message}. {failure_hint}"

    return {
        "running": running,
        "method": selected_method,
        "pid": pid,
        "message": base_message,
    }


def stop_vpn(ovpn_path: str) -> Dict[str, object]:
    """Stop the OpenVPN tunnel if it is running."""
    resolved_path, attempted_paths, _ = _resolve_ovpn_file(ovpn_path)
    detection = find_openvpn()
    display_input = str(Path(ovpn_path)) if ovpn_path else "<default>"
    profile_source = resolved_path or Path(ovpn_path or settings.DEFAULT_OVPN_PATH)
    profile = _profile_name(profile_source)

    if not resolved_path:
        hint = ""
        if attempted_paths:
            hint = f" Checked: {', '.join(attempted_paths)}."
        return {
            "running": False,
            "method": detection["method"],
            "pid": None,
            "message": f"Configuration file not found: {display_input}{hint}",
        }

    method, error_message = _choose_method(detection, profile, resolved_path)
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
        reachable = running and _tcp_reachable(OPENVPN_HOST, OPENVPN_PORT)
        if running:
            message = "OpenVPN GUI still reports a running session after disconnect command."
        else:
            message = "Disconnect command issued via GUI."
        return {
            "running": running,
            "reachable": reachable,
            "method": "gui",
            "pid": pid,
            "message": message,
        }
    elif detection["method"] == "cli":
        state = _build_cli_state()
        if not state.process_running:
            message = state.last_error or "OpenVPN CLI does not appear to be running."
            if state.last_log_line and state.last_log_line not in (state.last_error or ""):
                message = f"{message} Last log line: {state.last_log_line}"
            return {
                "running": False,
                "reachable": state.reachable,
                "method": "cli",
                "pid": None,
                "message": message,
            }

        try:
            process = psutil.Process(state.pid) if state.pid else None
            if process:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except psutil.TimeoutExpired:
                    process.kill()
        except psutil.NoSuchProcess:
            pass

        _remove_pid_file()
        new_state = _build_cli_state()
        message = "VPN CLI process terminated"
        return {
            "running": False,
            "reachable": new_state.reachable,
            "method": "cli",
            "pid": None,
            "message": message,
        }
    else:
        return {
            "running": False,
            "reachable": False,
            "method": "missing",
            "pid": None,
            "message": "OpenVPN executable not found.",
        }


def vpn_status(ovpn_path: str) -> Dict[str, object]:
    """Return the current status of the VPN connection."""
    resolved_path, attempted_paths, _ = _resolve_ovpn_file(ovpn_path)
    detection = find_openvpn()
    display_input = str(Path(ovpn_path)) if ovpn_path else "<default>"
    profile_source = resolved_path or Path(ovpn_path or settings.DEFAULT_OVPN_PATH)
    profile = _profile_name(profile_source)

    if not resolved_path:
        hint = ""
        if attempted_paths:
            hint = f" Checked: {', '.join(attempted_paths)}."
        return {
            "running": False,
            "method": detection["method"],
            "pid": None,
            "message": f"Configuration file not found: {display_input}{hint}",
        }

    method, error_message = _choose_method(detection, profile, resolved_path)
    if method == "missing":
        return {
            "running": False,
            "method": detection["method"],
            "pid": None,
            "message": error_message or "OpenVPN executable not found.",
        }

    pid: Optional[int] = None
    running = False

    if detection["method"] == "gui":
        process = _find_openvpn_process_for_profile(profile)
        running = process is not None
        pid = process.pid if process else None
        reachable = running and _tcp_reachable(OPENVPN_HOST, OPENVPN_PORT)
        if running and reachable:
            message = "VPN connected via GUI."
        elif running:
            message = (
                "OpenVPN GUI process detected but VPN health check host"
                f" {OPENVPN_HOST}:{OPENVPN_PORT} is unreachable."
            )
        else:
            message = "VPN not connected."
        return {
            "running": running,
            "reachable": reachable,
            "method": detection["method"],
            "pid": pid,
            "message": message,
        }

    if detection["method"] == "cli":
        state = _build_cli_state()
        if state.connected:
            message = (
                "VPN connected via CLI."
                if state.reachable
                else (
                    "VPN connected via CLI but health check host"
                    f" {OPENVPN_HOST}:{OPENVPN_PORT} is unreachable."
                )
            )
        else:
            message = state.last_error or "VPN not connected."
            if state.last_log_line and state.last_log_line not in (state.last_error or ""):
                message = f"{message} Last log line: {state.last_log_line}"

        return {
            "running": state.connected,
            "reachable": state.reachable,
            "method": detection["method"],
            "pid": state.pid,
            "message": message,
        }

    return {
        "running": False,
        "reachable": False,
        "method": detection["method"],
        "pid": None,
        "message": "OpenVPN executable not found.",
    }
