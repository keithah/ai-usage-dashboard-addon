"""Typed normalized models for the AI usage dashboard collector."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class SnapshotStatus(str, Enum):
    """Lifecycle status of an account snapshot.

    - fresh: data was retrieved successfully in this run.
    - stale: transient failure; last valid values are preserved.
    - auth_error: credentials missing/invalid (never a scrape, never fatal).
    - unsupported: provider metric has no stable documented API.
    - error: non-transient failure (malformed response, config error).
    """

    FRESH = "fresh"
    STALE = "stale"
    AUTH_ERROR = "auth_error"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class Unit(str, Enum):
    """Normalized metric units. Currency units are intentionally granular so
    that aggregation can refuse to add mixed currencies together."""

    USD = "usd"
    CNY = "cny"
    CREDITS = "credits"
    TOKENS = "tokens"
    REQUESTS = "requests"
    PERCENT = "percent"
    SECONDS = "seconds"
    COUNT = "count"


CURRENCY_UNITS = frozenset({Unit.USD, Unit.CNY})


@dataclass(frozen=True)
class Window:
    """Explicit time window a metric applies to."""

    kind: str  # e.g. "rolling_1h", "rolling_7d", "calendar_month", "total"
    label: str  # human label, e.g. "rolling 7d"
    start: str | None = None  # ISO-8601 timestamps, optional
    end: str | None = None


@dataclass(frozen=True)
class Metric:
    """One normalized provider metric."""

    key: str  # stable snake_case key, e.g. "spend_usd"
    label: str  # human label, e.g. "Spend (USD)"
    value: float | int | None
    unit: Unit
    window: Window | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "unit": self.unit.value,
        }
        if self.window is not None:
            d["window"] = {
                "kind": self.window.kind,
                "label": self.window.label,
                "start": self.window.start,
                "end": self.window.end,
            }
        return d


@dataclass(frozen=True)
class CredentialRef:
    """A reference to a secret; never a raw secret value."""

    env: str | None = None
    keychain_service: str | None = None
    keychain_account: str | None = None

    def describe(self) -> str:
        if self.env:
            return f"env:{self.env}"
        if self.keychain_service:
            return f"keychain:{self.keychain_service}/{self.keychain_account or ''}"
        return "missing"

    def is_empty(self) -> bool:
        return not self.env and not self.keychain_service


@dataclass(frozen=True)
class AccountConfig:
    provider: str
    account_id: str  # stable id within provider, e.g. "openai_primary"
    display_name: str
    credential: CredentialRef = field(default_factory=CredentialRef)
    options: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.account_id}"


@dataclass
class AccountSnapshot:
    provider: str
    account_id: str
    display_name: str
    status: SnapshotStatus
    reason: str = ""
    metrics: list[Metric] = field(default_factory=list)
    fetched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.account_id}"

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "account_id": self.account_id,
            "display_name": self.display_name,
            "status": self.status.value,
            "reason": self.reason,
            "fetched_at": self.fetched_at,
            "metrics": [m.to_dict() for m in self.metrics],
        }


class CurrencyMismatchError(ValueError):
    """Raised when an aggregation would mix different currencies."""


def sum_same_currency(metrics: list[Metric]) -> tuple[float, Unit]:
    """Sum metric values; refuse to add different currencies together."""
    if not metrics:
        raise ValueError("no metrics to sum")
    units = {m.unit for m in metrics}
    if len(units) > 1:
        raise CurrencyMismatchError(
            f"refusing to sum mixed units: {sorted(u.value for u in units)}"
        )
    total = sum(float(m.value) for m in metrics if m.value is not None)
    return total, metrics[0].unit
