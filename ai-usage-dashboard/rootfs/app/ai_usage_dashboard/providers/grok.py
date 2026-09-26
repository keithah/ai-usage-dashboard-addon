"""Grok (xAI) adapter: usage/billing via the management API.

xAI provides a management API for billing and usage at
``https://management-api.x.ai/v1/billing/teams/{team_id}/usage``. This endpoint
requires a management API key (different from inference API keys) and returns
usage data aggregated over a time period.

The management API is separate from the inference API (``api.x.ai``). Users must
create a management key in the xAI console with appropriate permissions.

Options::

    team_id: "team_..."      # required; your xAI team ID
    usage_url: "..."         # optional; override the default usage endpoint
    start_date: "2026-01-01" # optional; query start date (ISO format)
    end_date: "2026-01-31"   # optional; query end date (ISO format)

Credential handling: the management API key is resolved from the configured
credential reference (env var or keychain) and passed as a Bearer token.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..models import AccountConfig, AccountSnapshot, Metric, SnapshotStatus, Unit, Window
from .base import CollectContext
from ._helpers import _Auth, as_number, classify_http, fresh, resolve_live_credential, terminal

provider_name = "grok"

DEFAULT_USAGE_URL = "https://management-api.x.ai/v1/billing/teams/{team_id}/usage"


class Adapter:
    provider_name = "grok"

    def collect(self, account: AccountConfig, ctx: CollectContext) -> AccountSnapshot:
        if ctx.fixture_mode:
            return _from_fixture(account, ctx.account_fixture)

        team_id = str(account.options.get("team_id", "")).strip()
        if not team_id:
            return terminal(
                account,
                SnapshotStatus.ERROR,
                "Grok provider requires 'team_id' in options (your xAI team ID); "
                "find it in the xAI console under Team Settings",
            )

        try:
            secret = resolve_live_credential(account, ctx)
        except _Auth as exc:
            return terminal(account, SnapshotStatus.AUTH_ERROR, str(exc))

        usage_url = account.options.get("usage_url", DEFAULT_USAGE_URL)
        if "{team_id}" in usage_url:
            usage_url = usage_url.format(team_id=team_id)

        # Default to last 30 days if not specified
        now = datetime.now(timezone.utc)
        start_date = account.options.get("start_date")
        end_date = account.options.get("end_date")

        if not start_date:
            start_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = now.strftime("%Y-%m-%d")

        headers = {
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        }

        payload = {
            "start_date": start_date,
            "end_date": end_date,
            "granularity": "day",
        }

        try:
            resp = ctx.http.request("POST", usage_url, headers=headers, json=payload)
        except Exception as exc:
            return classify_http(account, exc)

        return _from_live(account, resp.body)

    def fixture_names(self) -> list[str]:
        return ["usage"]


def _from_fixture(account: AccountConfig, fixture: dict) -> AccountSnapshot:
    error = (fixture or {}).get("error")
    if error:
        return _snapshot_from_error(account, error)
    responses = (fixture or {}).get("responses", {})
    body = responses.get("usage", {})
    return _from_live(account, body)


def _from_live(account: AccountConfig, body: object) -> AccountSnapshot:
    """Parse xAI billing usage response.

    Expected shape (from xAI docs):
    {
        "data": [
            {
                "date": "2026-01-15",
                "cost": 12.34,
                "requests": 150,
                "input_tokens": 50000,
                "output_tokens": 25000
            },
            ...
        ],
        "total_cost": 345.67,
        "total_requests": 4500
    }
    """
    if not isinstance(body, dict):
        return terminal(
            account,
            SnapshotStatus.UNSUPPORTED,
            "Grok usage endpoint returned unexpected response format; "
            "check team_id and management API key permissions",
        )

    metrics: list[Metric] = []

    # Try to extract total cost
    total_cost = as_number(body.get("total_cost"))
    if total_cost is not None:
        metrics.append(
            Metric(
                "spend_usd",
                "Spend (USD)",
                round(total_cost, 4),
                Unit.USD,
                Window(kind="query_period", label="query period"),
            )
        )

    # Try to extract total requests
    total_requests = as_number(body.get("total_requests"))
    if total_requests is not None:
        metrics.append(
            Metric(
                "requests",
                "Total Requests",
                int(total_requests),
                Unit.REQUESTS,
                Window(kind="query_period", label="query period"),
            )
        )

    # Try to extract daily breakdown and sum if totals not available
    data = body.get("data")
    if isinstance(data, list) and data:
        if total_cost is None:
            cost_sum = 0.0
            for row in data:
                if isinstance(row, dict):
                    cost = as_number(row.get("cost"))
                    if cost is not None:
                        cost_sum += cost
            if cost_sum > 0:
                metrics.append(
                    Metric(
                        "spend_usd",
                        "Spend (USD)",
                        round(cost_sum, 4),
                        Unit.USD,
                        Window(kind="query_period", label="query period"),
                    )
                )

        if total_requests is None:
            req_sum = 0.0
            for row in data:
                if isinstance(row, dict):
                    reqs = as_number(row.get("requests"))
                    if reqs is not None:
                        req_sum += reqs
            if req_sum > 0:
                metrics.append(
                    Metric(
                        "requests",
                        "Total Requests",
                        int(req_sum),
                        Unit.REQUESTS,
                        Window(kind="query_period", label="query period"),
                    )
                )

    if not metrics:
        return terminal(
            account,
            SnapshotStatus.UNSUPPORTED,
            "Grok usage endpoint returned no usable data; "
            "check team_id, date range, and management API key permissions",
        )

    return fresh(account, metrics, "xAI Grok usage via management API")


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
