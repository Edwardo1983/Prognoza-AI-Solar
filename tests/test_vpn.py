
from pathlib import Path

import pytest

from app import settings, vpn


@pytest.fixture(autouse=True)
def clear_environment(monkeypatch):
    monkeypatch.delenv(vpn.PREFERRED_METHOD_ENV, raising=False)
    monkeypatch.delenv(vpn.CLI_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(vpn.GUI_OVERRIDE_ENV, raising=False)


@pytest.fixture
def temp_workspace(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    secrets_dir = workspace / "secrets"
    raw_dir = workspace / "data" / "raw"
    exports_dir = workspace / "data" / "exports"
    for directory in (secrets_dir, raw_dir, exports_dir):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "BASE_DIR", workspace)
    monkeypatch.setattr(settings, "DATA_DIR", workspace / "data")
    monkeypatch.setattr(settings, "RAW_DATA_DIR", raw_dir)
    monkeypatch.setattr(settings, "EXPORTS_DIR", exports_dir)
    monkeypatch.setattr(settings, "SECRETS_DIR", secrets_dir)
    log_file = raw_dir / "vpn.log"
    monkeypatch.setattr(settings, "VPN_LOG_FILE", log_file)
    default_ovpn = secrets_dir / "Prognoza-UMG-509-PRO.ovpn"
    monkeypatch.setattr(settings, "DEFAULT_OVPN_PATH", default_ovpn)
    return {
        "workspace": workspace,
        "secrets": secrets_dir,
        "raw": raw_dir,
        "log": log_file,
        "default": default_ovpn,
    }


def test_find_openvpn_prefers_cli_when_both_available(monkeypatch, temp_workspace):
    gui_path = Path('C:/Program Files/OpenVPN/bin/openvpn-gui.exe')
    cli_path = Path('C:/Program Files/OpenVPN/bin/openvpn.exe')
    monkeypatch.setattr(vpn, '_find_openvpn_gui', lambda: gui_path)
    monkeypatch.setattr(vpn, '_find_openvpn_cli', lambda hint=None: cli_path)

    result = vpn.find_openvpn()

    assert result['method'] == 'cli'
    assert result['gui_path'] == gui_path
    assert result['cli_path'] == cli_path


def test_start_vpn_falls_back_to_cli_when_gui_profile_missing(monkeypatch, temp_workspace):
    ovpn_file = temp_workspace['default']
    ovpn_file.write_text('client', encoding='utf-8')

    detection = {
        'method': 'gui',
        'gui_path': Path('C:/Program Files/OpenVPN/bin/openvpn-gui.exe'),
        'cli_path': Path('C:/Program Files/OpenVPN/bin/openvpn.exe'),
    }

    monkeypatch.setattr(vpn, 'find_openvpn', lambda: detection)
    monkeypatch.setattr(vpn, '_gui_profile_exists', lambda profile, path: False)
    monkeypatch.setattr(vpn, '_spawn_openvpn_cli', lambda path, cli: 4321)
    monkeypatch.setattr(vpn, '_read_pid_file', lambda: None)
    monkeypatch.setattr(vpn, '_is_process_alive', lambda pid: True)
    monkeypatch.setattr(vpn, '_tcp_reachable', lambda host, port, timeout=2.0: True)

    result = vpn.start_vpn(str(ovpn_file))

    assert result['method'] == 'cli'
    assert result['running'] is True
    assert result['pid'] == 4321


def test_start_vpn_requires_gui_profile_when_cli_unavailable(monkeypatch, temp_workspace):
    ovpn_file = temp_workspace['default']
    ovpn_file.write_text('client', encoding='utf-8')

    detection = {
        'method': 'gui',
        'gui_path': Path('C:/Program Files/OpenVPN/bin/openvpn-gui.exe'),
        'cli_path': None,
    }

    monkeypatch.setattr(vpn, 'find_openvpn', lambda: detection)
    monkeypatch.setattr(vpn, '_gui_profile_exists', lambda profile, path: False)

    result = vpn.start_vpn(str(ovpn_file))

    assert result['running'] is False
    assert result['method'] == 'gui'
    assert 'OpenVPN GUI profile' in result['message']


def test_start_vpn_recovers_from_filename_typo(monkeypatch, temp_workspace):
    ovpn_file = temp_workspace['secrets'] / 'Prognoza-UMG-509-PRO.ovpn'
    ovpn_file.write_text('client', encoding='utf-8')

    detection = {
        'method': 'gui',
        'gui_path': Path('C:/Program Files/OpenVPN/bin/openvpn-gui.exe'),
        'cli_path': Path('C:/Program Files/OpenVPN/bin/openvpn.exe'),
    }

    monkeypatch.setattr(vpn, 'find_openvpn', lambda: detection)
    monkeypatch.setattr(vpn, '_gui_profile_exists', lambda profile, path: False)
    monkeypatch.setattr(vpn, '_spawn_openvpn_cli', lambda path, cli: 9876)
    monkeypatch.setattr(vpn, '_read_pid_file', lambda: None)
    monkeypatch.setattr(vpn, '_is_process_alive', lambda pid: True)
    monkeypatch.setattr(vpn, '_tcp_reachable', lambda host, port, timeout=2.0: True)

    result = vpn.start_vpn('secrets/Prognoza-UMG509-PRO.ovpn')

    assert result['running'] is True
    assert result['method'] == 'cli'
    assert result['pid'] == 9876
    assert 'Resolved' in result['message']


def test_start_vpn_reports_missing_config_with_hints(monkeypatch, temp_workspace):
    detection = {
        'method': 'cli',
        'gui_path': None,
        'cli_path': Path('C:/Program Files/OpenVPN/bin/openvpn.exe'),
    }

    monkeypatch.setattr(vpn, 'find_openvpn', lambda: detection)

    result = vpn.start_vpn('missing-profile.ovpn')

    assert result['running'] is False
    assert 'Configuration file not found' in result['message']
    assert 'Checked:' in result['message']


def test_start_vpn_adds_admin_hint_when_access_denied(monkeypatch, temp_workspace):
    ovpn_file = temp_workspace['default']
    ovpn_file.write_text('client', encoding='utf-8')
    temp_workspace['log'].write_text(
        "2025-09-26 22:55:46 TUN: Setting IPv4 mtu failed: Access is denied.\n",
        encoding='utf-8',
    )

    detection = {
        'method': 'cli',
        'gui_path': None,
        'cli_path': Path('C:/Program Files/OpenVPN/bin/openvpn.exe'),
    }

    monkeypatch.setattr(vpn, 'find_openvpn', lambda: detection)
    monkeypatch.setattr(vpn, '_spawn_openvpn_cli', lambda path, cli: 1111)
    monkeypatch.setattr(vpn, '_read_pid_file', lambda: 1111)
    monkeypatch.setattr(vpn, '_is_process_alive', lambda pid: True)
    monkeypatch.setattr(vpn, '_tcp_reachable', lambda host, port, timeout=2.0: False)

    result = vpn.start_vpn(str(ovpn_file))

    assert result['running'] is False
    assert 'Access is denied' in result['message']
    assert 'Administrator' in result['message']
