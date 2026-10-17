"""Credential access for local macOS runtime storage."""
from __future__ import annotations

import subprocess


class CredentialError(RuntimeError):
    """Raised when a configured credential cannot be read."""


def _validate_argument(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(char) < 32 for char in value):
        raise CredentialError(f"{name} is invalid")
    return value


class KeychainCredentialStore:
    """Read OData passwords from macOS Keychain without exposing them in config."""

    def __init__(self, timeout_seconds: int = 10) -> None:
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be 1..60")
        self.timeout_seconds = timeout_seconds

    def password(self, service: str, user: str) -> str:
        service = _validate_argument(service, "service")
        user = _validate_argument(user, "user")
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-a", user, "-w"],
                capture_output=True,
                check=False,
                shell=False,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise CredentialError("macOS Keychain is unavailable") from None
        if result.returncode != 0:
            raise CredentialError("credential is unavailable in macOS Keychain")
        password = result.stdout.rstrip("\n")
        if password == "":
            raise CredentialError("credential is unavailable in macOS Keychain")
        return password
