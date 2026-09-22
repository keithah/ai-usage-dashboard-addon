"""OpenAI adapter: API usage/spend where documented and available.

OpenAI usage/billing endpoints vary by organization/project permissions, so
both endpoint URLs are configurable per account. Anything the configured key
cannot access is reported as unsupported (or auth_error for 401/403), never
scraped or guessed. Stateless per account: multiple configured accounts work
naturally.
"""
from __future__ import annotations

from ..models import AccountConfig, AccountSnapshot, Metric, SnapshotStatus, Unit, Window
from .base import CollectContext
from ._helpers import _Auth, as_number, classify_http, fresh, resolve_live_credential, terminal

provider_name = "openai"

DEFAULT_USAGE_URL = "https://api.openai.com/v1/organization/usage/completions"
DEFAULT_SUBSCRIPTION_URL = "https://api.openai.com/v1/organization/subscription"


class Adapter:
    provider_name = "openai"

    def collect(self, account: AccountConfig, ctx: CollectContext) -> AccountSnapshot:
        opts = account.options
        if ctx.fixture_mode:
            return _from_fixture(account, ctx.account_fixture)
        try:
            secret = resolve_live_credential(account, ctx)
        except _Auth as exc:
            return terminal(account, SnapshotStatus.AUTH_ERROR, str(exc))
        headers = {"Authorization": f"Bearer {secret}"}
        usage_url = opts.get("usage_url", DEFAULT_USAGE_URL)
        try:
            resp = ctx.http.get(usage_url, headers=headers)
        except Exception as exc:  # mapped below; transient ones propagate
            return classify_http(account, exc)
        return _from_live(account, resp.body, opts)

    def fixture_names(self) -> list[str]:
        return ["usage"]


def _from_fixture(account: AccountConfig, fixture: dict) -> AccountSnapshot:
    error = (fixture or {}).get("error")
    if error:
        return _snapshot_from_error(account, error)
    responses = (fixture or {}).get("responses", {})
    body = responses.get("usage", {})
    metrics, reason = _parse_usage(body)
    if metrics is None:
        return terminal(
            account, SnapshotStatus.ERROR,
            "fixture usage payload did not match the documented shape",
        )
    return fresh(account, metrics, reason)


def _from_live(account: AccountConfig, body: object, opts: dict) -> AccountSnapshot:
    metrics, reason = _parse_usage(body if isinstance(body, dict) else {})
    if metrics is None:
        # The key works but this endpoint/shape is not available to it.
        return terminal(
            account,
            SnapshotStatus.UNSUPPORTED,
            "OpenAI usage endpoint not accessible with this key/permissions; "
            "no usage data available (configure usage_url or check org permissions)",
        )
    return fresh(account, metrics, reason)


def _parse_usage(body: dict) -> tuple[list[Metric] | None, str]:
    """Parse the documented usage shape: {"data": [{"...spend/cost...}]}.

    Returns None when the payload does not match (unsupported/error path).
    """
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return None, ""
    total: float | None = None
    for row in body["data"]:
        if not isinstance(row, dict):
            continue
        for key in ("spend", "cost", "amount", "total_spend"):
            num = as_number(row.get(key))
            if num is not None:
                total = (total or 0.0) + num
    if total is None:
        # Shape matched (has data list) but no spend rows: valid zero-usage
        # only when explicitly marked; otherwise treat as unparseable.
        if body.get("data") == []:
            total = 0.0
        else:
            return None, ""
    window = Window(kind="calendar_month", label="current month")
    return [Metric(key="spend", label="Spend (USD)", value=round(total, 4), unit=Unit.USD, window=window)], ""


def _snapshot_from_error(account: AccountConfig, error: dict) -> AccountSnapshot:
    kind = error.get("kind", "error")
    message = error.get("message", "fixture error")
    if kind == "auth":
        return terminal(account, SnapshotStatus.AUTH_ERROR, message)
    if kind == "unsupported":
        return terminal(account, SnapshotStatus.UNSUPPORTED, message)
    if kind in ("transient", "rate_limit"):
        raise _Transient(message)
    return terminal(account, SnapshotStatus.ERROR, message)


class _Transient(Exception):
    pass
