
from pathlib import Path

import pytest

from app import vpn


@pytest.fixture(autouse=True)
def clear_environment(monkeypatch):
    monkeypatch.delenv(vpn.PREFERRED_METHOD_ENV, raising=False)
    monkeypatch.delenv(vpn.CLI_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(vpn.GUI_OVERRIDE_ENV, raising=False)


def test_find_openvpn_prefers_cli_when_both_available(monkeypatch):
    gui_path = Path('C:/Program Files/OpenVPN/bin/openvpn-gui.exe')
    cli_path = Path('C:/Program Files/OpenVPN/bin/openvpn.exe')
    monkeypatch.setattr(vpn, '_find_openvpn_gui', lambda: gui_path)
    monkeypatch.setattr(vpn, '_find_openvpn_cli', lambda hint=None: cli_path)

    result = vpn.find_openvpn()

    assert result['method'] == 'cli'
    assert result['gui_path'] == gui_path
    assert result['cli_path'] == cli_path


def test_start_vpn_falls_back_to_cli_when_gui_profile_missing(monkeypatch, tmp_path):
    ovpn_file = tmp_path / 'Prognoza-UMG-509-PRO.ovpn'
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


def test_start_vpn_requires_gui_profile_when_cli_unavailable(monkeypatch, tmp_path):
    ovpn_file = tmp_path / 'Prognoza-UMG-509-PRO.ovpn'
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
