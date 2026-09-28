"""OpenAI Codex OAuth adapter via the host-side metrics bridge."""
from __future__ import annotations

from ..models import AccountConfig, AccountSnapshot, Metric, SnapshotStatus, Unit, Window
from ._helpers import fresh, terminal
from ._oauth_bridge import fetch_bridge_document, metric_name
from .base import CollectContext

provider_name = "codex_oauth"


class Adapter:
    provider_name = provider_name

    def collect(self, account: AccountConfig, ctx: CollectContext) -> AccountSnapshot:
        if ctx.fixture_mode:
            return _from_body(account, ctx.account_fixture.get("responses", {}).get("rate_limits", {}))
        document = fetch_bridge_document(account, ctx, "/v1/codex/rate-limits")
        if isinstance(document, AccountSnapshot):
            return document
        return _from_body(account, document)

    def fixture_names(self) -> list[str]:
        return ["rate_limits"]


def _from_body(account: AccountConfig, body: dict) -> AccountSnapshot:
    if body.get("authenticated") is False:
        return terminal(
            account,
            SnapshotStatus.AUTH_ERROR,
            "Codex OAuth session is not authenticated; sign in again on the bridge host",
        )
    raw_limits = body.get("rate_limits")
    if not isinstance(raw_limits, list) or not raw_limits:
        return terminal(
            account,
            SnapshotStatus.UNSUPPORTED,
            "Codex OAuth bridge returned no rate-limit windows",
        )
    metrics: list[Metric] = []
    seen: set[str] = set()
    for item in raw_limits:
        if not isinstance(item, dict):
            continue
        name = metric_name(item.get("name"))
        if name in seen:
            continue
        seen.add(name)
        window = Window(
            kind=f"codex_{name}",
            label=f"Codex {name} rate limit",
        )
        for suffix, unit, key in (
            ("used_percent", Unit.PERCENT, "used_percent"),
            ("remaining_percent", Unit.PERCENT, "remaining_percent"),
            ("window_seconds", Unit.SECONDS, "window_seconds"),
        ):
            value = item.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf"):
                metrics.append(Metric(
                    key=f"{name}_{suffix}",
                    label=f"{name.replace('_', ' ').title()} {suffix.replace('_', ' ')}",
                    value=value,
                    unit=unit,
                    window=window,
                ))
    if not metrics:
        return terminal(account, SnapshotStatus.UNSUPPORTED, "Codex OAuth rate-limit payload was empty")
    plan = body.get("plan_type")
    reason = f"OAuth rate limits via Codex app-server ({plan})" if isinstance(plan, str) and plan else "OAuth rate limits via Codex app-server"
    return fresh(account, metrics, reason)
