"""Google Gemini provider: usage tracking via Cloud Billing API.

Google Gemini API usage can be tracked through the Google Cloud Billing API.
This requires a Google Cloud project with billing enabled and appropriate
service account credentials.

Setup instructions:
1. Create a Google Cloud project at https://console.cloud.google.com
2. Enable the Gemini API and Cloud Billing API
3. Create a service account with billing read permissions
4. Download the service account JSON key
5. Store the key path in secrets.env as GEMINI_BILLING_KEY_PATH
6. Configure the provider with your project ID and billing account ID

Note: This provider tracks billing data, not per-request usage. For detailed
per-request metrics, use the Gemini API's built-in usage tracking in responses.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..models import AccountConfig, AccountSnapshot, Metric, SnapshotStatus, Unit, Window
from .base import CollectContext
from ._helpers import _Auth, as_number, classify_http, fresh, resolve_live_credential, terminal

provider_name = "gemini"

DEFAULT_BILLING_URL = "https://cloudbilling.googleapis.com/v1/billingAccounts/{billing_account_id}/services/{service_id}/skus"


class Adapter:
    provider_name = "gemini"

    def collect(self, account: AccountConfig, ctx: CollectContext) -> AccountSnapshot:
        opts = account.options
        if ctx.fixture_mode:
            return _from_fixture(account, ctx.account_fixture)

        project_id = str(opts.get("project_id", "")).strip()
        billing_account_id = str(opts.get("billing_account_id", "")).strip()

        if not project_id:
            return terminal(
                account,
                SnapshotStatus.ERROR,
                "Gemini provider requires 'project_id' in options",
            )

        if not billing_account_id:
            return terminal(
                account,
                SnapshotStatus.ERROR,
                "Gemini provider requires 'billing_account_id' in options",
            )

        try:
            secret = resolve_live_credential(account, ctx)
        except _Auth as exc:
            return terminal(account, SnapshotStatus.AUTH_ERROR, str(exc))

        # Load service account credentials
        try:
            credentials = json.loads(secret)
        except json.JSONDecodeError as exc:
            return terminal(
                account,
                SnapshotStatus.AUTH_ERROR,
                f"Gemini credentials must be valid JSON: {exc}",
            )

        # For now, we'll use a simplified approach - in production, you'd use
        # google-auth library to get an access token from the service account
        # This is a placeholder that shows the structure
        headers = {
            "Authorization": f"Bearer {credentials.get('private_key', '')}",
            "Content-Type": "application/json",
        }

        # Query billing information
        billing_url = opts.get("billing_url", DEFAULT_BILLING_URL)
        if "{billing_account_id}" in billing_url:
            billing_url = billing_url.format(billing_account_id=billing_account_id)

        # Add project filter
        billing_url += f"?filter=project:{project_id}"

        try:
            resp = ctx.http.get(billing_url, headers=headers)
        except Exception as exc:
            return classify_http(account, exc)

        return _from_live(account, resp.body, project_id)

    def fixture_names(self) -> list[str]:
        return ["usage"]


def _from_fixture(account: AccountConfig, fixture: dict) -> AccountSnapshot:
    error = (fixture or {}).get("error")
    if error:
        return _snapshot_from_error(account, error)
    responses = (fixture or {}).get("responses", {})
    body = responses.get("usage", {})
    project_id = account.options.get("project_id", account.account_id)
    return _from_live(account, body, project_id)


def _from_live(account: AccountConfig, body: object, project_id: str) -> AccountSnapshot:
    """Parse billing response into metrics.

    Note: The actual Google Cloud Billing API response structure is complex.
    This is a simplified parser that extracts cost information.
    In production, you'd use the Cloud Billing API client library.
    """
    if not isinstance(body, dict):
        return terminal(
            account,
            SnapshotStatus.UNSUPPORTED,
            "Gemini billing endpoint returned unexpected response format",
        )

    metrics: list[Metric] = []

    # Extract cost information
    # The actual API returns a list of SKUs with pricing info
    # This is a simplified example
    total_cost = as_number(body.get("totalCost"))
    if total_cost is not None:
        metrics.append(
            Metric(
                key="monthly_cost",
                label="Monthly Cost (USD)",
                value=round(total_cost, 2),
                unit=Unit.USD,
                window=Window(kind="calendar_month", label="current month"),
            )
        )

    # Extract usage metrics if available
    usage = body.get("usage", {})
    if isinstance(usage, dict):
        prompt_tokens = as_number(usage.get("promptTokens"))
        completion_tokens = as_number(usage.get("completionTokens"))
        total_tokens = as_number(usage.get("totalTokens"))

        if prompt_tokens is not None:
            metrics.append(
                Metric(
                    key="prompt_tokens",
                    label="Prompt Tokens",
                    value=int(prompt_tokens),
                    unit=Unit.TOKENS,
                    window=Window(kind="calendar_month", label="current month"),
                )
            )

        if completion_tokens is not None:
            metrics.append(
                Metric(
                    key="completion_tokens",
                    label="Completion Tokens",
                    value=int(completion_tokens),
                    unit=Unit.TOKENS,
                    window=Window(kind="calendar_month", label="current month"),
                )
            )

        if total_tokens is not None:
            metrics.append(
                Metric(
                    key="total_tokens",
                    label="Total Tokens",
                    value=int(total_tokens),
                    unit=Unit.TOKENS,
                    window=Window(kind="calendar_month", label="current month"),
                )
            )

    if not metrics:
        return terminal(
            account,
            SnapshotStatus.UNSUPPORTED,
            "Gemini billing endpoint returned no usable data. "
            "Note: Full billing API integration requires google-auth library.",
        )

    reason = f"Project {project_id} billing data"
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
