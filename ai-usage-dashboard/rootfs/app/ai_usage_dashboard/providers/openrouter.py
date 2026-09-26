"""OpenRouter provider: account credits and usage tracking.

OpenRouter's management-key credits endpoint reports account-level purchased
credits and usage. It is the correct source for dashboard totals; a generation
endpoint only describes one immutable request and is not account usage.

Setup instructions:
1. Create an OpenRouter management key at https://openrouter.ai/settings/keys
2. Store it in secrets.env as OPENROUTER_API_KEY
3. Set ``mode: credits`` in the provider options (the default)
"""
from __future__ import annotations

from urllib.parse import urlsplit

from ..models import AccountConfig, AccountSnapshot, Metric, SnapshotStatus, Unit, Window
from .base import CollectContext
from ._helpers import _Auth, as_number, classify_http, fresh, resolve_live_credential, terminal

provider_name = "openrouter"
DEFAULT_CREDITS_URL = "https://openrouter.ai/api/v1/credits"


class Adapter:
    provider_name = "openrouter"

    def collect(self, account: AccountConfig, ctx: CollectContext) -> AccountSnapshot:
        if ctx.fixture_mode:
            return _from_fixture(account, ctx.account_fixture)
        try:
            secret = resolve_live_credential(account, ctx)
        except _Auth as exc:
            return terminal(account, SnapshotStatus.AUTH_ERROR, str(exc))

        headers = {"Authorization": f"Bearer {secret}"}
        url = str(account.options.get("credits_url", DEFAULT_CREDITS_URL)).strip()
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "openrouter.ai":
            return terminal(account, SnapshotStatus.ERROR, "OpenRouter credits_url must use HTTPS on openrouter.ai")
        try:
            resp = ctx.http.get(url, headers=headers)
        except Exception as exc:
            return classify_http(account, exc)
        return _from_live(account, resp.body)

    def fixture_names(self) -> list[str]:
        return ["usage"]


def _from_fixture(account: AccountConfig, fixture: dict) -> AccountSnapshot:
    error = (fixture or {}).get("error")
    if error:
        return _snapshot_from_error(account, error)
    body = (fixture or {}).get("responses", {}).get("usage", {})
    return _from_live(account, body)


def _from_live(account: AccountConfig, body: object) -> AccountSnapshot:
    if not isinstance(body, dict):
        return terminal(account, SnapshotStatus.UNSUPPORTED, "OpenRouter credits endpoint returned an unexpected response")
    data = body.get("data", body)
    if not isinstance(data, dict):
        return terminal(account, SnapshotStatus.UNSUPPORTED, "OpenRouter credits response missing data")

    total_credits = as_number(data.get("total_credits"))
    total_usage = as_number(data.get("total_usage"))
    if total_credits is None or total_usage is None:
        return terminal(account, SnapshotStatus.UNSUPPORTED, "OpenRouter credits response missing total_credits or total_usage")

    remaining = max(total_credits - total_usage, 0.0)
    window = Window(kind="account_lifetime", label="current account")
    metrics = [
        Metric("credits_purchased", "Credits Purchased (USD)", round(total_credits, 6), Unit.USD, window),
        Metric("usage", "Usage (USD)", round(total_usage, 6), Unit.USD, window),
        Metric("credits_remaining", "Credits Remaining (USD)", round(remaining, 6), Unit.USD, window),
    ]
    return fresh(account, metrics, "OpenRouter account credits")


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
