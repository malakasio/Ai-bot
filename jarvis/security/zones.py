"""Security zones module - security zone classification and validation.

This module provides security zone classification, command validation,
and PII sanitization functions.
"""

import os
import re


def can_access(path: str, write: bool = False) -> tuple[bool, str]:
    """Check if a path can be accessed based on security zones."""
    # Black zone - never accessible
    black_zones = ['/proc/', '/sys/', '/dev/']
    for zone in black_zones:
        if path.startswith(zone):
            return False, "Black zone - system critical path"

    # Red zone - system files
    red_zones = ['/etc/', '/boot/', '/root/', '/var/']
    for zone in red_zones:
        if path.startswith(zone) and write:
            return False, "Red zone - system files blocked for write"

    # Green zone - user workspace
    home = os.path.expanduser("~")
    if path.startswith(f"{home}/jarvis/workspace"):
        return True, "Green zone - allowed"

    return True, "Allowed"


def classify_path(path: str) -> str:
    """Classify a path into security zones: black, red, yellow, green."""
    if path.startswith('/proc/') or path.startswith('/sys/'):
        return "black"
    if path.startswith('/etc/') or path.startswith('/boot/'):
        return "red"
    return "green"


def validate_command(cmd: list[str]) -> tuple[bool, str]:
    """Validate if a command is allowed to execute."""
    if not cmd:
        return False, "Empty command"

    base_cmd = cmd[0]

    # Check for nmap without lab mode (check before blocked commands)
    if base_cmd == 'nmap':
        if os.getenv('JARVIS_LAB_MODE', 'false').lower() != 'true':
            return False, "nmap requires JARVIS_LAB_MODE=true"

    # Safe commands whitelist
    SAFE_COMMANDS = {
        'ls', 'pwd', 'cd', 'cat', 'head', 'tail', 'grep', 'find',
        'git', 'gh', 'docker', 'pytest', 'python', 'python3',
        'rg', 'ag', 'ack', 'tree', 'less', 'more', 'wc', 'sort',
        'uniq', 'diff', 'patch', 'file', 'stat', 'du', 'df'
    }

    # Blocked commands
    BLOCKED_COMMANDS = {
        'rm', 'rmdir', 'dd', 'mkfs', 'fdisk', 'chmod', 'chown',
        'echo', 'tee', 'nc', 'netcat', 'telnet'
    }

    if base_cmd in BLOCKED_COMMANDS:
        return False, f"Command '{base_cmd}' is blocked"

    # Check for -exec in find command
    if base_cmd == 'find' and '-exec' in cmd:
        return False, "find -exec is blocked (bypasses whitelist)"

    if base_cmd not in SAFE_COMMANDS:
        return False, f"Command '{base_cmd}' not in whitelist"

    return True, "Command allowed"


def sanitize_email_content(content: str) -> str:
    """Wrap email content in untrusted tags to prevent prompt injection."""
    return f"<untrusted_email_content>\n{content}\n</untrusted_email_content>"


def sanitize_pii(text: str) -> tuple[str, dict]:
    """Anonymize PII in text. Returns (anonymized_text, mapping)."""
    # Stub implementation - would use presidio in production
    mapping = {}
    anonymized = text

    # Simple email detection
    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    emails = re.findall(email_pattern, text)

    for i, email in enumerate(emails):
        placeholder = f"EMAIL_{i}"
        mapping[placeholder] = email
        anonymized = anonymized.replace(email, placeholder)

    return anonymized, mapping


def deanonymize(text: str, mapping: dict) -> str:
    """Restore original text from anonymized version."""
    result = text
    for placeholder, original in mapping.items():
        result = result.replace(placeholder, original)
    return result


__all__ = [
    'can_access',
    'classify_path',
    'validate_command',
    'sanitize_email_content',
    'sanitize_pii',
    'deanonymize',
]
