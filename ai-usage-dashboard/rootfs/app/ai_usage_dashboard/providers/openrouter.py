"""OpenRouter provider: usage and cost tracking via the generation API.

OpenRouter provides a `/api/v1/generation/{generation_id}` endpoint that returns
detailed usage statistics including token counts and cost for completed generations.

Setup instructions:
1. Sign up at https://openrouter.ai
2. Create an API key at https://openrouter.ai/keys
3. Store the API key in secrets.env as OPENROUTER_API_KEY
4. Configure the provider with the generation IDs you want to track

Note: This provider tracks individual generation costs. For aggregate usage,
you may need to query multiple generation IDs or use OpenRouter's dashboard.
"""
from __future__ import annotations

from ..models import AccountConfig, AccountSnapshot, Metric, SnapshotStatus, Unit, Window
from .base import CollectContext
from ._helpers import _Auth, as_number, classify_http, fresh, resolve_live_credential, terminal

provider_name = "openrouter"

DEFAULT_GENERATION_URL = "https://openrouter.ai/api/v1/generation/{generation_id}"


class Adapter:
    provider_name = "openrouter"

    def collect(self, account: AccountConfig, ctx: CollectContext) -> AccountSnapshot:
        opts = account.options
        if ctx.fixture_mode:
            return _from_fixture(account, ctx.account_fixture)

        generation_id = str(opts.get("generation_id", account.account_id)).strip()
        if not generation_id:
            return terminal(
                account,
                SnapshotStatus.ERROR,
                "OpenRouter provider requires 'generation_id' in options or account_id",
            )

        try:
            secret = resolve_live_credential(account, ctx)
        except _Auth as exc:
            return terminal(account, SnapshotStatus.AUTH_ERROR, str(exc))

        headers = {
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        }

        generation_url = opts.get("generation_url", DEFAULT_GENERATION_URL)
        if "{generation_id}" in generation_url:
            generation_url = generation_url.format(generation_id=generation_id)

        try:
            resp = ctx.http.get(generation_url, headers=headers)
        except Exception as exc:
            return classify_http(account, exc)

        return _from_live(account, resp.body, generation_id)

    def fixture_names(self) -> list[str]:
        return ["usage"]


def _from_fixture(account: AccountConfig, fixture: dict) -> AccountSnapshot:
    error = (fixture or {}).get("error")
    if error:
        return _snapshot_from_error(account, error)
    responses = (fixture or {}).get("responses", {})
    body = responses.get("usage", {})
    generation_id = account.account_id
    return _from_live(account, body, generation_id)


def _from_live(account: AccountConfig, body: object, generation_id: str) -> AccountSnapshot:
    """Parse generation response into metrics.

    Expected shape:
    {
        "data": {
            "id": "generation-xxxxx",
            "model": "openai/gpt-4",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150
            },
            "cost": 0.00123
        }
    }
    """
    if not isinstance(body, dict):
        return terminal(
            account,
            SnapshotStatus.UNSUPPORTED,
            "OpenRouter generation endpoint returned unexpected response format",
        )

    metrics: list[Metric] = []
    data = body.get("data", body)  # Some endpoints wrap in "data", others don't

    if not isinstance(data, dict):
        return terminal(
            account,
            SnapshotStatus.UNSUPPORTED,
            "OpenRouter generation response missing 'data' object",
        )

    # Extract usage information
    usage = data.get("usage", {})
    if isinstance(usage, dict):
        prompt_tokens = as_number(usage.get("prompt_tokens"))
        completion_tokens = as_number(usage.get("completion_tokens"))
        total_tokens = as_number(usage.get("total_tokens"))

        if prompt_tokens is not None:
            metrics.append(
                Metric(
                    key="prompt_tokens",
                    label="Prompt Tokens",
                    value=int(prompt_tokens),
                    unit=Unit.TOKENS,
                    window=Window(kind="generation", label=generation_id),
                )
            )

        if completion_tokens is not None:
            metrics.append(
                Metric(
                    key="completion_tokens",
                    label="Completion Tokens",
                    value=int(completion_tokens),
                    unit=Unit.TOKENS,
                    window=Window(kind="generation", label=generation_id),
                )
            )

        if total_tokens is not None:
            metrics.append(
                Metric(
                    key="total_tokens",
                    label="Total Tokens",
                    value=int(total_tokens),
                    unit=Unit.TOKENS,
                    window=Window(kind="generation", label=generation_id),
                )
            )

    # Extract cost information
    cost = as_number(data.get("cost"))
    if cost is not None:
        metrics.append(
            Metric(
                key="cost",
                label="Cost (USD)",
                value=round(cost, 6),
                unit=Unit.USD,
                window=Window(kind="generation", label=generation_id),
            )
        )

    if not metrics:
        return terminal(
            account,
            SnapshotStatus.UNSUPPORTED,
            "OpenRouter generation endpoint returned no usable data",
        )

    model = data.get("model", "unknown")
    reason = f"Generation {generation_id} using model {model}"

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
