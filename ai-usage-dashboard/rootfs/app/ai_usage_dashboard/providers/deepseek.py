"""DeepSeek adapter: documented user balance endpoint.

GET https://api.deepseek.com/user/balance returns available/granted/topped-up
style balances, i.e. {"is_available": ..., "balance_infos": [{"currency",
"total_balance", "granted_balance", "topped_up_balance"}]}. Parsed
conservatively; currency is taken from the payload, never assumed.
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

provider_name = "deepseek"

DEFAULT_BALANCE_URL = "https://api.deepseek.com/user/balance"


class Adapter:
    provider_name = "deepseek"

    def collect(self, account: AccountConfig, ctx: CollectContext) -> AccountSnapshot:
        if ctx.fixture_mode:
            return _from_fixture(account, ctx.account_fixture)
        try:
            secret = resolve_live_credential(account, ctx)
        except _Auth as exc:
            return terminal(account, SnapshotStatus.AUTH_ERROR, str(exc))
        url = account.options.get("balance_url", DEFAULT_BALANCE_URL)
        try:
            resp = ctx.http.get(url, headers={"Authorization": f"Bearer {secret}"})
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


def _unit_for(currency: str) -> Unit | None:
    normalized = (currency or "").strip().upper()
    if normalized == "CNY":
        return Unit.CNY
    if normalized == "USD":
        return Unit.USD
    return None


def _from_body(account: AccountConfig, body: dict) -> AccountSnapshot:
    infos = body.get("balance_infos") if isinstance(body, dict) else None
    if not isinstance(infos, list) or not infos:
        return terminal(
            account, SnapshotStatus.UNSUPPORTED,
            "DeepSeek balance payload did not match the documented "
            "balance_infos shape",
        )
    metrics: list[Metric] = []
    for info in infos:
        if not isinstance(info, dict):
            continue
        unit = _unit_for(str(info.get("currency", "")))
        if unit is None:
            continue  # unknown currency: skip rather than mislabel
        window = Window(kind="total", label="current balance")
        for key, label in (
            ("total_balance", "Total balance"),
            ("granted_balance", "Granted balance"),
            ("topped_up_balance", "Topped-up balance"),
        ):
            num = as_number(info.get(key))
            if num is not None:
                metrics.append(
                    Metric(
                        key=f"balance_{key}_{unit.value}",
                        label=f"{label} ({unit.value.upper()})",
                        value=num,
                        unit=unit,
                        window=window,
                    )
                )
    if not metrics:
        return terminal(
            account, SnapshotStatus.ERROR,
            "DeepSeek balance_infos present but no usable balances parsed",
        )
    reason = ""
    if body.get("is_available") is False:
        reason = "DeepSeek reports balance not available"
    return fresh(account, metrics, reason)


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
