"""Credential resolution from references (never from inline values)."""
from __future__ import annotations

import os
import subprocess

from .models import CredentialRef


class CredentialNotFound(LookupError):
    pass


def resolve(ref: CredentialRef, *, account_key: str) -> str:
    """Resolve a credential reference to a secret string.

    Resolution order:
    1. Environment variable (if ref.env is set)
    2. Direct value (if ref.value is set)
    3. Keychain (if ref.keychain_service is set)

    The secret is returned only to the in-memory caller and must never be
    written to state topics, discovery payloads, logs, or fixtures.
    """
    # Try environment variable first
    if ref.env:
        value = os.environ.get(ref.env, "")
        if value:
            return value
        # If env var is set but empty, continue to next method
    
    # Try direct value
    if ref.value:
        return ref.value
    
    # Try keychain
    if ref.keychain_service:
        return _read_keychain(ref, account_key=account_key)
    
    # If env was set but not found, give specific error
    if ref.env:
        raise CredentialNotFound(
            f"{account_key}: env var {ref.env!r} is unset or empty, "
            "and no alternative credential source configured"
        )
    
    raise CredentialNotFound(
        f"{account_key}: no credential reference configured "
        "(expected env, value, or keychain_service)"
    )


def _read_keychain(ref: CredentialRef, *, account_key: str) -> str:
    cmd = [
        "security",
        "find-generic-password",
        "-s",
        ref.keychain_service or "",
        "-w",
    ]
    if ref.keychain_account:
        cmd.extend(["-a", ref.keychain_account])
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CredentialNotFound(
            f"{account_key}: keychain lookup failed: {type(exc).__name__}"
        ) from exc
    if proc.returncode != 0 or not proc.stdout.strip():
        raise CredentialNotFound(
            f"{account_key}: keychain entry "
            f"{ref.keychain_service!r}/{ref.keychain_account or ''} not found"
        )
    return proc.stdout.strip()
