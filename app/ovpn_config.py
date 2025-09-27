"""Helpers for preparing OpenVPN configuration profiles for Windows GUI usage."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List
import re

INLINE_SECTIONS = {
    "ca": "ca.crt",
    "cert": "client.crt",
    "key": "client.key",
    "tls-auth": "ta.key",
    "tls-crypt": "ta.key",
}


def parse_ovpn_file(path: Path) -> Dict[str, object]:
    """Return metadata and the raw text contents of an OpenVPN profile."""
    text = path.read_text(encoding="utf-8")
    return {"path": path, "text": text}


def extract_certificates(config_text: str, out_dir: Path) -> Dict[str, Path]:
    """Extract inline certificate blocks into separate files within ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    assets: Dict[str, Path] = {}

    for tag, filename in INLINE_SECTIONS.items():
        pattern = re.compile(rf"<{tag}>(.*?)</{tag}>", re.IGNORECASE | re.DOTALL)
        match = pattern.search(config_text)
        if not match:
            continue
        content = match.group(1).strip()
        file_path = out_dir / filename
        file_path.write_text(f"{content}\n", encoding="utf-8")
        assets[tag] = file_path

    return assets


def generate_clean_config(original_text: str, assets_dir: Path, umg_ip: str, profile_name: str) -> str:
    """Generate a GUI-friendly OpenVPN profile referencing extracted assets."""
    inline_assets = extract_certificates(original_text, assets_dir)
    effective_lines: List[str] = []
    skip_tag: str | None = None
    opening_tokens = {f"<{tag}>": tag for tag in INLINE_SECTIONS}

    for line in original_text.splitlines():
        trimmed_lower = line.strip().lower()
        if skip_tag:
            if trimmed_lower == f"</{skip_tag}>":
                skip_tag = None
            continue
        section_tag = opening_tokens.get(trimmed_lower)
        if section_tag and section_tag in inline_assets:
            skip_tag = section_tag
            continue
        effective_lines.append(line)

    # Remove legacy file references that will be replaced.
    def _remove_existing(keyword: str) -> None:
        nonlocal effective_lines
        effective_lines = [
            candidate
            for candidate in effective_lines
            if not candidate.strip().lower().startswith(f"{keyword} ")
        ]

    mapping = {
        "ca": "ca ca.crt",
        "cert": "cert client.crt",
        "key": "key client.key",
        "tls-auth": "tls-auth ta.key 1",
        "tls-crypt": "tls-crypt ta.key",
    }

    for key in ("ca", "cert", "key", "tls-auth", "tls-crypt"):
        if key in inline_assets:
            directive = mapping[key]
            _remove_existing(directive.split()[0])
            effective_lines.append(directive)

    optimization_keywords = (
        'dev ',
        'proto ',
        'cipher ',
        'data-ciphers',
        'auth ',
        'comp-lzo',
        'compress',
        'resolv-retry',
        'ping ',
        'ping-restart',
        'ping-timer-rem',
        'server-poll-timeout',
        'explicit-exit-notify',
        'setenv opt',
        'tun-mtu',
        'mssfix',
    )
    effective_lines = [
        line
        for line in effective_lines
        if not any(line.strip().lower().startswith(keyword) for keyword in optimization_keywords)
    ]

    def _ensure_directive(value: str) -> None:
        lower_value = value.lower()
        if not any(existing.strip().lower() == lower_value for existing in effective_lines):
            effective_lines.append(value)

    route_directive = f"route {umg_ip} 255.255.255.255"
    if not any(line.strip().lower().startswith(f"route {umg_ip.lower()}") for line in effective_lines):
        effective_lines.append(route_directive)

    optimized_directives = [
        'client',
        'dev tun',
        'proto udp',
        'nobind',
        'remote-cert-tls server',
        'resolv-retry infinite',
        'setenv opt block-outside-dns',
        'cipher AES-256-GCM',
        'ncp-ciphers AES-256-GCM:AES-128-GCM:AES-256-CBC',
        'data-ciphers AES-256-GCM:AES-128-GCM:AES-256-CBC',
        'data-ciphers-fallback AES-256-CBC',
        'tun-mtu 1500',
        'mssfix 1360',
        'ping 10',
        'ping-restart 60',
        'ping-timer-rem',
        'server-poll-timeout 10',
        'explicit-exit-notify 2',
    ]

    for directive in optimized_directives:
        _ensure_directive(directive)

    clean_text = "\n".join(dict.fromkeys(effective_lines)).strip() + "\n"
    return clean_text


def write_clean_files(clean_text: str, assets_dir: Path, profile_name: str) -> Path:
    """Persist the generated configuration to disk and return its path."""
    assets_dir.mkdir(parents=True, exist_ok=True)
    profile_path = assets_dir / f"{profile_name}.ovpn"
    profile_path.write_text(clean_text, encoding="utf-8")
    return profile_path