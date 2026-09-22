"""Shared helpers for provider adapters."""
from __future__ import annotations

from datetime import datetime, timezone

from .. import credentials as credentials_mod
from ..http_client import HttpAuthError, HttpError, HttpRateLimitError, HttpTransientError
from ..models import AccountConfig, AccountSnapshot, SnapshotStatus
from .base import CollectContext


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fresh(account: AccountConfig, metrics, reason: str = "") -> AccountSnapshot:
    return AccountSnapshot(
        provider=account.provider,
        account_id=account.account_id,
        display_name=account.display_name,
        status=SnapshotStatus.FRESH,
        reason=reason,
        metrics=list(metrics),
        fetched_at=now_iso(),
    )


def terminal(
    account: AccountConfig, status: SnapshotStatus, reason: str
) -> AccountSnapshot:
    return AccountSnapshot(
        provider=account.provider,
        account_id=account.account_id,
        display_name=account.display_name,
        status=status,
        reason=reason,
        metrics=[],
        fetched_at=now_iso(),
    )


def resolve_live_credential(account: AccountConfig, ctx: CollectContext) -> str:
    if ctx.fixture_mode:
        return "fixture-credential"
    try:
        return credentials_mod.resolve(account.credential, account_key=account.key)
    except LookupError as exc:
        raise _Auth(str(exc)) from exc


class _Auth(Exception):
    pass


def classify_http(account: AccountConfig, exc: Exception) -> AccountSnapshot:
    """Map HTTP/client failures to snapshots (transient ones are retriable)."""
    if isinstance(exc, _Auth):
        return terminal(account, SnapshotStatus.AUTH_ERROR, str(exc))
    if isinstance(exc, HttpAuthError):
        return terminal(account, SnapshotStatus.AUTH_ERROR, str(exc))
    if isinstance(exc, HttpRateLimitError):
        raise exc  # retriable: collector may preserve last valid as stale
    if isinstance(exc, HttpTransientError):
        raise exc
    if isinstance(exc, HttpError):
        return terminal(account, SnapshotStatus.ERROR, str(exc))
    return terminal(account, SnapshotStatus.ERROR, f"{type(exc).__name__}: {exc}")


def as_number(value: object) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return num
