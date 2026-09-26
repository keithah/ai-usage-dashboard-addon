"""OpenCode Go adapter: no live quota polling.

Setup Instructions:
1. Install the `opencode` CLI from https://opencode.ai
2. Authenticate with your Go subscription credentials
3. Note: This provider only tracks local session statistics, not subscription quota

Authoritative findings: the official docs expose authenticated web-console
usage at https://opencode.ai/auth plus model/inference endpoints, but do
not document an account quota endpoint. ``opencode stats`` is documented
for local token/cost statistics, not remaining Go subscription quota, and
no stable machine-readable option for it is verified here.

This adapter therefore makes no network calls (never the undocumented
``/usage`` endpoint) and never labels local token/cost stats as remaining
Go subscription quota. Collection always reports ``unsupported`` with a
clear reason that distinguishes ``local_session_usage`` from
``subscription_quota`` and points at the official web console. Provider
registration and config-only account additions are preserved: an account
reports ``unsupported`` with diagnostics instead of silently becoming
zero.

Options::

    mode: local_stats    # explicit; documents that only local stats exist

``cli_path`` (default ``opencode``) and ``timeout`` are accepted and
validated as reserved options for a future verified invocation, but no
CLI is executed until a stable machine-readable option is
documented/verified. Legacy ``usage_url``/``quota_url`` options were
removed with the undocumented endpoint and are rejected as config errors
with migration guidance.
"""
from __future__ import annotations

from ..models import AccountConfig, AccountSnapshot, SnapshotStatus
from .base import CollectContext
from ._helpers import terminal

provider_name = "opencode_go"

OFFICIAL_CONSOLE_URL = "https://opencode.ai/auth"
DEFAULT_CLI_PATH = "opencode"
DEFAULT_TIMEOUT = 30.0

_LEGACY_OPTIONS = ("usage_url", "quota_url")


class Adapter:
    provider_name = "opencode_go"

    def collect(self, account: AccountConfig, ctx: CollectContext) -> AccountSnapshot:
        legacy = [k for k in _LEGACY_OPTIONS if k in account.options]
        if legacy:
            return terminal(
                account, SnapshotStatus.ERROR,
                "OpenCode Go no longer polls a usage endpoint "
                f"({', '.join(sorted(legacy))} were removed because no "
                "account quota endpoint is documented); set options "
                "mode: local_stats and check subscription usage in the "
                f"official web console: {OFFICIAL_CONSOLE_URL}. "
                "`opencode stats` reports local_session_usage only, never "
                "remaining subscription_quota.",
            )
        if ctx.fixture_mode:
            error = (ctx.account_fixture or {}).get("error")
            if error:
                return _snapshot_from_error(account, error)
        mode = str(account.options.get("mode", "")).strip().lower()
        if mode and mode != "local_stats":
            return terminal(
                account, SnapshotStatus.ERROR,
                f"unknown mode {account.options.get('mode')!r}; expected "
                "'local_stats' (`opencode stats` reports local "
                "token/cost statistics, not remaining Go subscription quota)",
            )
        for name in ("cli_path", "timeout"):
            problem = _validate_reserved_option(account, name)
            if problem is not None:
                return problem
        if not mode:
            return terminal(
                account, SnapshotStatus.UNSUPPORTED,
                "OpenCode Go subscription_quota is unavailable: no account "
                "quota endpoint is documented (set options mode: local_stats "
                "to document the account; local stats still cannot report "
                "remaining quota). Subscription usage lives in the official "
                f"web console: {OFFICIAL_CONSOLE_URL}.",
            )
        return terminal(
            account, SnapshotStatus.UNSUPPORTED,
            "OpenCode Go local_stats mode: `opencode stats` reports local "
            "token/cost statistics (local_session_usage), not remaining Go "
            "subscription_quota, and no stable machine-readable option is "
            "verified, so no local CLI is executed. Subscription usage lives "
            f"in the official web console: {OFFICIAL_CONSOLE_URL}.",
        )

    def fixture_names(self) -> list[str]:
        return []


def _validate_reserved_option(
    account: AccountConfig, name: str
) -> AccountSnapshot | None:
    """Validate (but do not use) reserved CLI options for a future verified run."""
    if name not in account.options:
        return None
    value = account.options.get(name)
    if name == "cli_path":
        if not str(value).strip():
            return terminal(
                account, SnapshotStatus.ERROR,
                "OpenCode Go 'cli_path' must be a non-empty executable path "
                f"(default {DEFAULT_CLI_PATH!r}); reserved for a future "
                "verified `opencode stats` invocation",
            )
        return None
    try:
        timeout = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        timeout = -1.0
    if timeout <= 0 or timeout > 3600:
        return terminal(
            account, SnapshotStatus.ERROR,
            f"OpenCode Go 'timeout' must be a positive number of seconds "
            f"(got {value!r}); reserved for a future verified "
            "`opencode stats` invocation",
        )
    return None


def _snapshot_from_error(account: AccountConfig, error: dict) -> AccountSnapshot:
    kind = error.get("kind", "error")
    message = error.get("message", "fixture error")
    if kind == "auth":
        return terminal(account, SnapshotStatus.AUTH_ERROR, message)
    if kind == "unsupported":
        return terminal(account, SnapshotStatus.UNSUPPORTED, message)
    if kind in ("transient", "rate_limit"):
        from .openai import _Transient

        raise _Transient(message)
    return terminal(account, SnapshotStatus.ERROR, message)
