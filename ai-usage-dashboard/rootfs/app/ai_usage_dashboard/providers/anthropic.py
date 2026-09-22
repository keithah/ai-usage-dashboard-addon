"""Anthropic/Claude adapter: usage/rate-limit or spend where documented.

Documented inputs: rate-limit response headers and (with an admin key) the
usage/cost reporting endpoints. Exact subscription quota is unavailable via a
stable documented API, so it is never scraped from Claude.ai; when only
rate-limit data exists the snapshot reports that and marks subscription
quota unsupported in the reason. Stateless per account.
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

provider_name = "anthropic"

DEFAULT_USAGE_URL = "https://api.anthropic.com/v1/organizations/usage_report"
RATE_LIMIT_HEADERS = (
    "anthropic-ratelimit-requests-limit",
    "anthropic-ratelimit-requests-remaining",
    "anthropic-ratelimit-tokens-limit",
    "anthropic-ratelimit-tokens-remaining",
)


class Adapter:
    provider_name = "anthropic"

    def collect(self, account: AccountConfig, ctx: CollectContext) -> AccountSnapshot:
        if ctx.fixture_mode:
            return _from_fixture(account, ctx.account_fixture)
        try:
            secret = resolve_live_credential(account, ctx)
        except _Auth as exc:
            return terminal(account, SnapshotStatus.AUTH_ERROR, str(exc))
        headers = {
            "x-api-key": secret,
            "anthropic-version": account.options.get("anthropic_version", "2023-06-01"),
        }
        url = account.options.get("usage_url", DEFAULT_USAGE_URL)
        try:
            resp = ctx.http.get(url, headers=headers)
        except Exception as exc:
            return classify_http(account, exc)
        return _from_live(account, resp.body, resp.headers)

    def fixture_names(self) -> list[str]:
        return ["usage"]


def _from_fixture(account: AccountConfig, fixture: dict) -> AccountSnapshot:
    error = (fixture or {}).get("error")
    if error:
        return _snapshot_from_error(account, error)
    responses = (fixture or {}).get("responses", {})
    headers = (fixture or {}).get("headers", {})
    body = responses.get("usage", {})
    return _from_live(account, body, headers)


def _from_live(
    account: AccountConfig, body: object, headers: dict
) -> AccountSnapshot:
    metrics: list[Metric] = []
    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
    remaining = as_number(lowered.get("anthropic-ratelimit-requests-remaining"))
    limit = as_number(lowered.get("anthropic-ratelimit-requests-limit"))
    if remaining is not None or limit is not None:
        window = Window(kind="rolling_1h", label="rate-limit window")
        if limit is not None:
            metrics.append(
                Metric("requests_limit", "Requests limit", limit, Unit.REQUESTS, window)
            )
        if remaining is not None:
            metrics.append(
                Metric("requests_remaining", "Requests remaining", remaining, Unit.REQUESTS, window)
            )
    spend = None
    if isinstance(body, dict):
        for key in ("total_spend", "spend", "cost", "amount"):
            spend = as_number(body.get(key))
            if spend is not None:
                break
    if spend is not None:
        metrics.append(
            Metric(
                "spend", "Spend (USD)", round(spend, 4), Unit.USD,
                Window(kind="calendar_month", label="current month"),
            )
        )
    if not metrics:
        return terminal(
            account,
            SnapshotStatus.UNSUPPORTED,
            "No documented Anthropic usage/rate-limit data available to this key; "
            "subscription quota is not exposed by a stable API (no Claude.ai scraping)",
        )
    reason = (
        "rate-limit data only; subscription quota unsupported via documented API"
        if spend is None
        else ""
    )
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
