"""CodeRabbit provider: usage tracking via the CodeRabbit API.

CodeRabbit provides a metrics API that returns review statistics including
complexity scores, review times, and comment counts.

Setup instructions:
1. Sign up at https://coderabbit.ai
2. Create an API key at https://coderabbit.ai/settings/api
3. Store the API key in secrets.env as CODERABBIT_API_KEY
4. Configure the provider with your organization ID

Note: This provider tracks review metrics, not billing/cost data. For billing
information, use the CodeRabbit dashboard directly.
"""
from __future__ import annotations

from ..models import AccountConfig, AccountSnapshot, Metric, SnapshotStatus, Unit, Window
from .base import CollectContext
from ._helpers import _Auth, as_number, classify_http, fresh, resolve_live_credential, terminal

provider_name = "coderabbit"

DEFAULT_METRICS_URL = "https://api.coderabbit.ai/api/v1/metrics"


class Adapter:
    provider_name = "coderabbit"

    def collect(self, account: AccountConfig, ctx: CollectContext) -> AccountSnapshot:
        opts = account.options
        if ctx.fixture_mode:
            return _from_fixture(account, ctx.account_fixture)

        organization_id = str(opts.get("organization_id", account.account_id)).strip()
        if not organization_id:
            return terminal(
                account,
                SnapshotStatus.ERROR,
                "CodeRabbit provider requires 'organization_id' in options or account_id",
            )

        try:
            secret = resolve_live_credential(account, ctx)
        except _Auth as exc:
            return terminal(account, SnapshotStatus.AUTH_ERROR, str(exc))

        headers = {
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        }

        metrics_url = opts.get("metrics_url", DEFAULT_METRICS_URL)
        metrics_url += f"?organization_id={organization_id}"

        try:
            resp = ctx.http.get(metrics_url, headers=headers)
        except Exception as exc:
            return classify_http(account, exc)

        return _from_live(account, resp.body, organization_id)

    def fixture_names(self) -> list[str]:
        return ["usage"]


def _from_fixture(account: AccountConfig, fixture: dict) -> AccountSnapshot:
    error = (fixture or {}).get("error")
    if error:
        return _snapshot_from_error(account, error)
    responses = (fixture or {}).get("responses", {})
    body = responses.get("usage", {})
    organization_id = account.options.get("organization_id", account.account_id)
    return _from_live(account, body, organization_id)


def _from_live(account: AccountConfig, body: object, organization_id: str) -> AccountSnapshot:
    """Parse metrics response into metrics.

    Expected shape:
    {
        "data": {
            "total_reviews": 150,
            "avg_complexity_score": 7.5,
            "avg_review_time_minutes": 12.3,
            "total_comments": 450,
            "period": "last_30_days"
        }
    }
    """
    if not isinstance(body, dict):
        return terminal(
            account,
            SnapshotStatus.UNSUPPORTED,
            "CodeRabbit metrics endpoint returned unexpected response format",
        )

    metrics: list[Metric] = []
    data = body.get("data", body)

    if not isinstance(data, dict):
        return terminal(
            account,
            SnapshotStatus.UNSUPPORTED,
            "CodeRabbit metrics response missing 'data' object",
        )

    period = data.get("period", "last_30_days")
    window = Window(kind="rolling_30d", label=period)

    # Extract review metrics
    total_reviews = as_number(data.get("total_reviews"))
    if total_reviews is not None:
        metrics.append(
            Metric(
                key="total_reviews",
                label="Total Reviews",
                value=int(total_reviews),
                unit=Unit.COUNT,
                window=window,
            )
        )

    avg_complexity = as_number(data.get("avg_complexity_score"))
    if avg_complexity is not None:
        metrics.append(
            Metric(
                key="avg_complexity_score",
                label="Average Complexity Score",
                value=round(avg_complexity, 2),
                unit=Unit.COUNT,
                window=window,
            )
        )

    avg_review_time = as_number(data.get("avg_review_time_minutes"))
    if avg_review_time is not None:
        metrics.append(
            Metric(
                key="avg_review_time_seconds",
                label="Average Review Time (seconds)",
                value=round(avg_review_time * 60, 2),
                unit=Unit.SECONDS,
                window=window,
            )
        )

    total_comments = as_number(data.get("total_comments"))
    if total_comments is not None:
        metrics.append(
            Metric(
                key="total_comments",
                label="Total Comments",
                value=int(total_comments),
                unit=Unit.COUNT,
                window=window,
            )
        )

    if not metrics:
        return terminal(
            account,
            SnapshotStatus.UNSUPPORTED,
            "CodeRabbit metrics endpoint returned no usable data",
        )

    reason = f"Organization {organization_id} review metrics"
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
