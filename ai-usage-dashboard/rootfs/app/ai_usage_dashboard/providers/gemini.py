"""Google Gemini cost tracking from a Google Cloud billing export.

Google's Cloud Billing REST API exposes catalog prices, not accrued spend. To
report real Gemini cost, enable a Cloud Billing export to BigQuery and give the
service account BigQuery Job User plus BigQuery Data Viewer access.

Setup instructions:
1. Enable Cloud Billing export to BigQuery:
   https://cloud.google.com/billing/docs/how-to/export-data-bigquery
2. Create a service account with BigQuery Job User and BigQuery Data Viewer
3. Create/download its JSON key
4. Store the JSON content in secrets.env as GEMINI_SERVICE_ACCOUNT_KEY
5. Set project_id and billing_export_table in provider options
"""
from __future__ import annotations

import base64
import json
import re
import time
from urllib.parse import urlencode, quote

from ..http_client import HttpAuthError, HttpError, HttpRateLimitError, HttpTransientError
from ..models import AccountConfig, AccountSnapshot, Metric, SnapshotStatus, Unit, Window
from .base import CollectContext
from ._helpers import _Auth, as_number, classify_http, fresh, resolve_live_credential, terminal

provider_name = "gemini"
DEFAULT_TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_BQ_URL = "https://bigquery.googleapis.com/bigquery/v2/projects/{project_id}/queries"
_ALLOWED_TOKEN_URLS = {DEFAULT_TOKEN_URL, "https://www.googleapis.com/oauth2/v4/token"}
_TABLE_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


class Adapter:
    provider_name = "gemini"

    def collect(self, account: AccountConfig, ctx: CollectContext) -> AccountSnapshot:
        opts = account.options
        if ctx.fixture_mode:
            return _from_fixture(account, ctx.account_fixture)
        project_id = str(opts.get("project_id", "")).strip()
        export_table = str(opts.get("billing_export_table", "")).strip()
        if not project_id:
            return terminal(account, SnapshotStatus.ERROR, "Gemini provider requires 'project_id' in options")
        if not _PROJECT_RE.fullmatch(project_id):
            return terminal(account, SnapshotStatus.ERROR, "Gemini project_id is not a valid Google Cloud project ID")
        if not _TABLE_RE.fullmatch(export_table):
            return terminal(
                account,
                SnapshotStatus.ERROR,
                "Gemini provider requires billing_export_table as project.dataset.table",
            )
        try:
            secret = resolve_live_credential(account, ctx)
            credentials = json.loads(secret)
            if not isinstance(credentials, dict):
                return terminal(account, SnapshotStatus.AUTH_ERROR, "Gemini credentials must be a JSON object")
        except _Auth as exc:
            return terminal(account, SnapshotStatus.AUTH_ERROR, str(exc))
        except json.JSONDecodeError as exc:
            return terminal(account, SnapshotStatus.AUTH_ERROR, f"Gemini credentials error: invalid JSON ({exc})")

        try:
            access_token = _get_access_token(credentials, ctx)
        except HttpAuthError as exc:
            return terminal(account, SnapshotStatus.AUTH_ERROR, str(exc))
        except (HttpRateLimitError, HttpTransientError):
            raise
        except HttpError as exc:
            # OAuth errors like invalid_grant return 400, mapped to base HttpError
            return terminal(account, SnapshotStatus.AUTH_ERROR, f"Gemini credentials error: {exc}")
        except (KeyError, ValueError, RuntimeError) as exc:
            return terminal(account, SnapshotStatus.AUTH_ERROR, f"Gemini credentials error: {exc}")

        query = _billing_query(export_table)
        try:
            response = _run_bigquery(ctx, project_id, access_token, query)
        except Exception as exc:
            return classify_http(account, exc)
        return _from_live(account, response.body, project_id)

    def fixture_names(self) -> list[str]:
        return ["usage"]


def _run_bigquery(ctx: CollectContext, project_id: str, access_token: str, query: str):
    headers = {"Authorization": f"Bearer {access_token}"}
    response = ctx.http.request(
        "POST",
        DEFAULT_BQ_URL.format(project_id=quote(project_id, safe="")),
        headers=headers,
        json={"query": query, "useLegacySql": False, "timeoutMs": 10000},
    )
    body = response.body
    if not isinstance(body, dict) or body.get("jobComplete") is not False:
        return response
    reference = body.get("jobReference")
    if not isinstance(reference, dict) or not reference.get("jobId"):
        raise HttpTransientError("Gemini BigQuery query did not complete and returned no job reference")
    location = reference.get("location")
    poll_url = (
        "https://bigquery.googleapis.com/bigquery/v2/projects/"
        f"{quote(project_id, safe='')}/queries/{quote(str(reference['jobId']), safe='')}"
    )
    poll_params = {"timeoutMs": "2000"}
    if location:
        poll_params["location"] = str(location)
    poll_url += "?" + urlencode(poll_params)
    for _ in range(3):
        sleep = getattr(ctx.http, "sleep", time.sleep)
        sleep(1)
        polled = ctx.http.get(poll_url, headers=headers)
        if isinstance(polled.body, dict) and polled.body.get("jobComplete") is not False:
            return polled
    raise HttpTransientError("Gemini BigQuery query remained incomplete after polling")


def _billing_query(export_table: str) -> str:
    return (
        "SELECT currency, "
        "COALESCE(SUM(cost), 0) + COALESCE(SUM((SELECT SUM(c.amount) FROM UNNEST(credits) c)), 0) AS total_cost, "
        "COUNT(*) AS usage_rows "
        f"FROM `{export_table}` "
        "WHERE invoice.month = FORMAT_DATE('%Y%m', CURRENT_DATE('America/Los_Angeles')) "
        "AND (service.description LIKE '%Gemini%' "
        "OR service.description LIKE '%Generative Language%' "
        "OR (service.description LIKE '%Vertex AI%' AND LOWER(sku.description) LIKE '%gemini%')) "
        "GROUP BY currency"
    )


def _get_access_token(credentials: dict, ctx: CollectContext) -> str:
    """Create a service-account JWT and exchange it through SafeHttpClient."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    token_url = str(credentials.get("token_uri", DEFAULT_TOKEN_URL))
    if token_url not in _ALLOWED_TOKEN_URLS:
        raise ValueError("token_uri must be a Google OAuth token endpoint")
    for field in ("client_email", "private_key"):
        if not credentials.get(field):
            raise KeyError(field)

    now = int(time.time())
    claims = {
        "iss": credentials["client_email"],
        "scope": "https://www.googleapis.com/auth/cloud-platform",
        "aud": token_url,
        "exp": now + 3600,
        "iat": now,
    }
    header = {"alg": "RS256", "typ": "JWT"}

    def encoded(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header_b64 = encoded(header)
    claims_b64 = encoded(claims)
    private_key = serialization.load_pem_private_key(
        credentials["private_key"].encode(), password=None
    )
    signing_input = f"{header_b64}.{claims_b64}".encode()
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    assertion = f"{header_b64}.{claims_b64}." + base64.urlsafe_b64encode(signature).decode().rstrip("=")
    response = ctx.http.request(
        "POST",
        token_url,
        form={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
    )
    if not isinstance(response.body, dict) or not response.body.get("access_token"):
        raise RuntimeError("Google OAuth response did not contain an access token")
    return str(response.body["access_token"])


def _from_fixture(account: AccountConfig, fixture: dict) -> AccountSnapshot:
    error = (fixture or {}).get("error")
    if error:
        return _snapshot_from_error(account, error)
    body = (fixture or {}).get("responses", {}).get("usage", {})
    return _from_live(account, body, str(account.options.get("project_id", account.account_id)))


def _from_live(account: AccountConfig, body: object, project_id: str) -> AccountSnapshot:
    if not isinstance(body, dict):
        return terminal(account, SnapshotStatus.UNSUPPORTED, "Gemini BigQuery response was not an object")
    if body.get("jobComplete") is False:
        raise HttpTransientError("Gemini BigQuery query did not complete before timeout")
    rows = body.get("rows", [])
    if not isinstance(rows, list):
        return terminal(account, SnapshotStatus.ERROR, "Gemini BigQuery response rows had an unexpected shape")
    if not rows:
        # No billing data for current month - return zero cost, not unsupported
        currency = str(account.options.get("currency", "USD")).upper()
        unit = {"USD": Unit.USD, "CNY": Unit.CNY}.get(currency)
        if unit is None:
            return terminal(account, SnapshotStatus.UNSUPPORTED, f"Configured currency {currency!r} is not supported")
        window = Window(kind="calendar_month", label="current month")
        metrics = [
            Metric("monthly_cost", f"Monthly Cost ({currency})", 0.0, unit, window),
            Metric("billing_rows", "Billing Rows", 0, Unit.COUNT, window),
        ]
        return fresh(account, metrics, f"Gemini billing export for project {project_id} (no usage this month)")
    if len(rows) != 1:
        return terminal(account, SnapshotStatus.ERROR, "Gemini billing export returned mixed currencies")
    values = rows[0].get("f", []) if isinstance(rows[0], dict) else []
    if not isinstance(values, list):
        return terminal(account, SnapshotStatus.ERROR, "Gemini BigQuery row had an unexpected shape")
    currency = str(values[0].get("v", "")).upper() if len(values) > 0 and isinstance(values[0], dict) else ""
    unit = {"USD": Unit.USD, "CNY": Unit.CNY}.get(currency)
    if unit is None:
        return terminal(account, SnapshotStatus.UNSUPPORTED, "Gemini billing export returned an unsupported or missing currency")
    total_cost = as_number(values[1].get("v")) if len(values) > 1 and isinstance(values[1], dict) else None
    usage_rows = as_number(values[2].get("v")) if len(values) > 2 and isinstance(values[2], dict) else None
    if total_cost is None:
        return terminal(account, SnapshotStatus.UNSUPPORTED, "Gemini billing export response missing total cost")
    window = Window(kind="calendar_month", label="current month")
    metrics = [Metric("monthly_cost", f"Monthly Cost ({currency})", round(total_cost, 6), unit, window)]
    if usage_rows is not None:
        metrics.append(Metric("billing_rows", "Billing Rows", int(usage_rows), Unit.COUNT, window))
    return fresh(account, metrics, f"Gemini billing export for project {project_id}")


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
