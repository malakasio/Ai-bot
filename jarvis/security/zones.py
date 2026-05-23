"""Security zones module - re-export from __init__ for compatibility."""

from . import classify_path, can_access, validate_command

__all__ = ['classify_path', 'can_access', 'validate_command']
