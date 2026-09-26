"""CodeRabbit provider: review usage metrics via the documented Metrics API.

The Metrics Data API returns one record per eligible merged pull request. This
adapter aggregates the current rolling window into review count, complexity,
estimated review minutes, and CodeRabbit comment counts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

from ..models import AccountConfig, AccountSnapshot, Metric, SnapshotStatus, Unit, Window
from .base import CollectContext
from ._helpers import _Auth, as_number, classify_http, fresh, resolve_live_credential, terminal

provider_name = "coderabbit"
DEFAULT_METRICS_URL = "https://api.coderabbit.ai/v1/metrics"


def _organization_id(account: AccountConfig) -> str:
    options = account.options
    return str(options.get("org_id") or options.get("organization_id") or account.account_id).strip()


class Adapter:
    provider_name = "coderabbit"

    def collect(self, account: AccountConfig, ctx: CollectContext) -> AccountSnapshot:
        if ctx.fixture_mode:
            return _from_fixture(account, ctx.account_fixture)
        options = account.options
        organization_id = _organization_id(account)
        if not organization_id:
            return terminal(account, SnapshotStatus.ERROR, "CodeRabbit provider requires organization_id or account_id")
        try:
            secret = resolve_live_credential(account, ctx)
        except _Auth as exc:
            return terminal(account, SnapshotStatus.AUTH_ERROR, str(exc))

        try:
            days = int(options.get("days", 30))
        except (TypeError, ValueError):
            return terminal(account, SnapshotStatus.ERROR, "CodeRabbit 'days' must be an integer")
        if not 1 <= days <= 3650:
            return terminal(account, SnapshotStatus.ERROR, "CodeRabbit 'days' must be between 1 and 3650")
        try:
            # Resolve end_date first, then derive start_date from it
            end_date = datetime.fromisoformat(str(options.get("end_date", datetime.now(timezone.utc).date()))).date()
            start_date = datetime.fromisoformat(str(options.get("start_date", end_date - timedelta(days=days)))).date()
        except ValueError:
            return terminal(account, SnapshotStatus.ERROR, "CodeRabbit start_date and end_date must be YYYY-MM-DD")
        if end_date < start_date:
            return terminal(account, SnapshotStatus.ERROR, "CodeRabbit end_date must not precede start_date")
        try:
            limit = int(options.get("limit", 1000))
        except (TypeError, ValueError):
            return terminal(account, SnapshotStatus.ERROR, "CodeRabbit 'limit' must be an integer")
        if not 1 <= limit <= 1000:
            return terminal(account, SnapshotStatus.ERROR, "CodeRabbit 'limit' must be between 1 and 1000")
        query = {
            "start_date": str(start_date),
            "end_date": str(end_date),
            "limit": str(limit),
            "org_id": organization_id,
        }
        metrics_url = str(options.get("metrics_url", DEFAULT_METRICS_URL))
        parsed_url = urlsplit(metrics_url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "api.coderabbit.ai":
            return terminal(account, SnapshotStatus.ERROR, "CodeRabbit metrics_url must use HTTPS on api.coderabbit.ai")
        headers = {"x-coderabbitai-api-key": secret}
        rows = []
        cursor = None
        seen_cursors = set()
        try:
            for _ in range(100):
                page_query = dict(query)
                if cursor:
                    page_query["cursor"] = cursor
                page_url = metrics_url + ("&" if "?" in metrics_url else "?") + urlencode(page_query)
                response = ctx.http.get(page_url, headers=headers)
                if not isinstance(response.body, dict) or not isinstance(response.body.get("data"), list):
                    return terminal(account, SnapshotStatus.UNSUPPORTED, "CodeRabbit Metrics API returned an unexpected response")
                rows.extend(response.body["data"])
                next_cursor = response.body.get("next_cursor")
                if not next_cursor:
                    break  # Normal end of pagination
                if next_cursor == cursor or next_cursor in seen_cursors:
                    return terminal(account, SnapshotStatus.ERROR, "CodeRabbit Metrics API returned a repeated cursor")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            else:
                return terminal(account, SnapshotStatus.ERROR, "CodeRabbit Metrics API pagination exceeded 100 pages")
        except Exception as exc:
            return classify_http(account, exc)
        return _from_live(account, {"data": rows}, organization_id, query["start_date"], query["end_date"])

    def fixture_names(self) -> list[str]:
        return ["usage"]


def _from_fixture(account: AccountConfig, fixture: dict) -> AccountSnapshot:
    error = (fixture or {}).get("error")
    if error:
        return _snapshot_from_error(account, error)
    body = (fixture or {}).get("responses", {}).get("usage", {})
    start = str(account.options.get("start_date", "fixture-start"))
    end = str(account.options.get("end_date", "fixture-end"))
    return _from_live(account, body, _organization_id(account), start, end)


def _from_live(account: AccountConfig, body: object, organization_id: str, start: str, end: str) -> AccountSnapshot:
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return terminal(account, SnapshotStatus.UNSUPPORTED, "CodeRabbit Metrics API returned an unexpected response")
    rows = [row for row in body["data"] if isinstance(row, dict)]
    try:
        start_date = datetime.fromisoformat(start).date()
        end_date = datetime.fromisoformat(end).date()
        span_days = (end_date - start_date).days
        window_start = start_date.isoformat()
        window_end = end_date.isoformat()
    except ValueError:
        span_days = 30
        window_start = None
        window_end = None
    window_kind = "calendar_day" if span_days == 0 else f"rolling_{span_days}d"
    window = Window(kind=window_kind, label=f"{start} through {end}", start=window_start, end=window_end)
    complexities = [as_number(row.get("estimated_complexity")) for row in rows if isinstance(row, dict)]
    complexities = [value for value in complexities if value is not None]
    review_minutes = [as_number(row.get("estimated_review_minutes")) for row in rows if isinstance(row, dict)]
    review_minutes = [value for value in review_minutes if value is not None]
    posted_comments = 0
    accepted_comments = 0
    for row in rows:
        comments = row.get("coderabbit_comments", {}) if isinstance(row, dict) else {}
        totals = comments.get("total", {}) if isinstance(comments, dict) else {}
        posted_comments += int(as_number(totals.get("posted")) or 0)
        accepted_comments += int(as_number(totals.get("accepted")) or 0)

    metrics = [Metric("total_reviews", "Total Reviews", len(rows), Unit.COUNT, window)]
    if complexities:
        metrics.append(Metric("average_complexity", "Average Complexity", round(sum(complexities) / len(complexities), 2), Unit.COUNT, window))
    if review_minutes:
        metrics.append(Metric("estimated_review_seconds", "Estimated Review Time (seconds)", round(sum(review_minutes) * 60, 2), Unit.SECONDS, window))
    metrics.extend([
        Metric("comments_posted", "Comments Posted", posted_comments, Unit.COUNT, window),
        Metric("comments_accepted", "Comments Accepted", accepted_comments, Unit.COUNT, window),
    ])
    return fresh(account, metrics, f"CodeRabbit organization {organization_id} metrics")


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
