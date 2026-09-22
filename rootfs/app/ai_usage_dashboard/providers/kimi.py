"""Kimi (Moonshot Open Platform) adapter: documented balance endpoint.

Uses a configurable base URL (default the Open Platform host) and parses the
documented available/voucher/cash balance shape conservatively: any numeric
subset is accepted, unknown shapes yield unsupported/error rather than zeros.
"""
from __future__ import annotations

from ..models import (
    AccountConfig,
    AccountSnapshot,
    Metric,
    SnapshotStatus,
    Unit,
    Window,
)
from .base import CollectContext
from ._helpers import _Auth, as_number, classify_http, fresh, resolve_live_credential, terminal

provider_name = "kimi"

DEFAULT_BASE_URL = "https://platform.moonshot.ai"
DEFAULT_BALANCE_PATH = "/v1/users/balance"


class Adapter:
    provider_name = "kimi"

    def collect(self, account: AccountConfig, ctx: CollectContext) -> AccountSnapshot:
        if ctx.fixture_mode:
            return _from_fixture(account, ctx.account_fixture)
        try:
            secret = resolve_live_credential(account, ctx)
        except _Auth as exc:
            return terminal(account, SnapshotStatus.AUTH_ERROR, str(exc))
        base = account.options.get("base_url", DEFAULT_BASE_URL).rstrip("/")
        path = account.options.get("balance_path", DEFAULT_BALANCE_PATH)
        try:
            resp = ctx.http.get(
                f"{base}{path}", headers={"Authorization": f"Bearer {secret}"}
            )
        except Exception as exc:
            return classify_http(account, exc)
        return _from_body(account, resp.body if isinstance(resp.body, dict) else {})

    def fixture_names(self) -> list[str]:
        return ["balance"]


def _from_fixture(account: AccountConfig, fixture: dict) -> AccountSnapshot:
    error = (fixture or {}).get("error")
    if error:
        return _snapshot_from_error(account, error)
    body = (fixture or {}).get("responses", {}).get("balance", {})
    return _from_body(account, body if isinstance(body, dict) else {})


def _from_body(account: AccountConfig, body: dict) -> AccountSnapshot:
    # Documented shape: {"code": 0, "data": {"available": .., "voucher": ..,
    # "cash": ..}} — accept data at top level too, case/conservatively.
    data = body.get("data", body) if isinstance(body, dict) else {}
    if not isinstance(data, dict):
        return terminal(
            account, SnapshotStatus.ERROR, "Kimi balance payload is not an object"
        )
    metrics: list[Metric] = []
    for key, label in (
        ("available", "Available balance"),
        ("voucher", "Voucher balance"),
        ("cash", "Cash balance"),
    ):
        num = as_number(data.get(key))
        if num is not None:
            metrics.append(
                Metric(
                    key=f"balance_{key}",
                    label=f"{label} (CNY)",
                    value=num,
                    unit=Unit.CNY,
                    window=Window(kind="total", label="current balance"),
                )
            )
    if not metrics:
        if isinstance(body, dict) and body.get("code", 0) not in (0, None):
            return terminal(
                account, SnapshotStatus.ERROR,
                f"Kimi balance API returned code={body.get('code')}",
            )
        return terminal(
            account, SnapshotStatus.UNSUPPORTED,
            "Kimi balance payload did not match the documented "
            "available/voucher/cash shape",
        )
    return fresh(account, metrics)


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
