"""Claude Code OAuth adapter via host-side transcript aggregation."""
from __future__ import annotations

from ..models import AccountConfig, AccountSnapshot, Metric, SnapshotStatus, Unit, Window
from ._helpers import fresh, terminal
from ._oauth_bridge import fetch_bridge_document
from .base import CollectContext

provider_name = "claude_code_oauth"


class Adapter:
    provider_name = provider_name

    def collect(self, account: AccountConfig, ctx: CollectContext) -> AccountSnapshot:
        if ctx.fixture_mode:
            return _from_body(account, ctx.account_fixture.get("responses", {}).get("usage", {}))
        document = fetch_bridge_document(account, ctx, "/v1/claude/usage")
        if isinstance(document, AccountSnapshot):
            return document
        return _from_body(account, document)

    def fixture_names(self) -> list[str]:
        return ["usage"]


def _from_body(account: AccountConfig, body: dict) -> AccountSnapshot:
    if body.get("authenticated") is False:
        return terminal(
            account,
            SnapshotStatus.AUTH_ERROR,
            "Claude Code OAuth session is not authenticated; sign in again on the bridge host",
        )
    raw = body.get("metrics")
    if not isinstance(raw, dict):
        return terminal(account, SnapshotStatus.UNSUPPORTED, "Claude OAuth bridge returned no usage metrics")
    window = Window(kind="calendar_month", label="current month")
    metrics: list[Metric] = []
    for key, label in (
        ("input_tokens", "Input tokens"),
        ("output_tokens", "Output tokens"),
        ("cache_creation_input_tokens", "Cache creation input tokens"),
        ("cache_read_input_tokens", "Cache read input tokens"),
        ("total_tokens", "Total tokens"),
        ("session_files", "Session files"),
    ):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf"):
            metrics.append(Metric(key, label, value, Unit.TOKENS if key != "session_files" else Unit.COUNT, window))
    if not metrics:
        return terminal(account, SnapshotStatus.UNSUPPORTED, "Claude OAuth usage payload was empty")
    return fresh(
        account,
        metrics,
        "Consumed local usage from OAuth Claude Code sessions; Claude Max remaining quota is not exposed by a supported API",
    )
