
import pytest

from ai_usage_dashboard.http_client import HttpAuthError, HttpResponse, SafeHttpClient
from ai_usage_dashboard.models import AccountConfig, CredentialRef, SnapshotStatus, Unit
from ai_usage_dashboard.providers._oauth_bridge import BridgeConfigError, bridge_endpoint, metric_name
from ai_usage_dashboard.providers.base import CollectContext
from ai_usage_dashboard.providers.claude_code_oauth import Adapter as ClaudeAdapter
from ai_usage_dashboard.providers.codex_oauth import Adapter as CodexAdapter

BRIDGE = "https://bridge.local:8768"


def _account(provider: str, **options) -> AccountConfig:
    opts = {"oauth_bridge_url": BRIDGE}
    opts.update(options)
    return AccountConfig(
        provider=provider,
        account_id="personal",
        display_name=provider,
        credential=CredentialRef(env="BRIDGE_TOKEN"),
        options=opts,
    )


@pytest.fixture
def bridge_token(monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", "bridge-secret")
    return "bridge-secret"


class _HTTP:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, headers=None, ca_file=None):
        self.calls.append((url, headers or {}, ca_file))
        return self.handler(url, headers or {}, ca_file)


def _ok(body):
    return lambda url, headers, ca_file: HttpResponse(status=200, headers={}, body=body)


def test_codex_oauth_adapter_maps_rate_limits_without_provider_token(bridge_token):
    http = _HTTP(_ok({
        "authenticated": True,
        "auth_mode": "oauth",
        "plan_type": "plus",
        "rate_limits": [
            {"name": "primary", "used_percent": 25, "remaining_percent": 75, "window_seconds": 18000, "reset_at": 1790000000}
        ],
    }))

    snapshot = CodexAdapter().collect(_account("codex_oauth"), CollectContext(http=http))

    assert http.calls == [(BRIDGE + "/v1/codex/rate-limits", {"Authorization": "Bearer bridge-secret"}, None)]
    assert snapshot.status is SnapshotStatus.FRESH
    assert [(m.key, m.value, m.unit) for m in snapshot.metrics] == [
        ("primary_used_percent", 25.0, Unit.PERCENT),
        ("primary_remaining_percent", 75.0, Unit.PERCENT),
        ("primary_window_seconds", 18000.0, Unit.SECONDS),
    ]


def test_claude_oauth_adapter_reports_consumed_local_usage(bridge_token):
    http = _HTTP(_ok({
        "authenticated": True,
        "auth_mode": "oauth",
        "window": "calendar_month",
        "metrics": {
            "input_tokens": 100,
            "output_tokens": 25,
            "cache_creation_input_tokens": 10,
            "cache_read_input_tokens": 5,
            "total_tokens": 140,
        },
    }))

    snapshot = ClaudeAdapter().collect(_account("claude_code_oauth"), CollectContext(http=http))

    assert http.calls[0][0] == BRIDGE + "/v1/claude/usage"
    assert snapshot.status is SnapshotStatus.FRESH
    assert "consumed local usage" in snapshot.reason.lower()
    assert snapshot.metrics[-1].key == "total_tokens"
    assert snapshot.metrics[-1].value == 140


@pytest.mark.parametrize("adapter_cls,provider", [(CodexAdapter, "codex_oauth"), (ClaudeAdapter, "claude_code_oauth")])
def test_missing_bridge_url_is_a_config_error_not_a_crash(bridge_token, adapter_cls, provider):
    http = _HTTP(_ok({}))
    account = _account(provider)
    account.options.pop("oauth_bridge_url")

    snapshot = adapter_cls().collect(account, CollectContext(http=http))

    assert snapshot.status is SnapshotStatus.ERROR
    assert "oauth_bridge_url is required" in snapshot.reason
    assert http.calls == []


@pytest.mark.parametrize("url", ["", "   ", "bridge.local:8768", "ftp://bridge.local", "https://", "not a url", "https://bridge.local:8768/api", "https://bridge.local:8768/?x=1", "https://bridge.local:8768#frag"])
def test_malformed_bridge_url_is_a_config_error(bridge_token, url):
    http = _HTTP(_ok({}))

    snapshot = CodexAdapter().collect(_account("codex_oauth", oauth_bridge_url=url), CollectContext(http=http))

    assert snapshot.status is SnapshotStatus.ERROR
    assert http.calls == []


@pytest.mark.parametrize("url", ["http://192.168.42.176:8768", "http://bridge.local:8768", "http://10.0.0.5"])
def test_plaintext_bridge_url_to_lan_host_is_rejected(bridge_token, url):
    http = _HTTP(_ok({}))

    snapshot = CodexAdapter().collect(_account("codex_oauth", oauth_bridge_url=url), CollectContext(http=http))

    assert snapshot.status is SnapshotStatus.ERROR
    assert "https://" in snapshot.reason
    assert http.calls == [], "bearer token must never be sent over plaintext LAN"


@pytest.mark.parametrize("url", ["http://127.0.0.1:8768", "http://localhost:8768", "http://[::1]:8768", "https://bridge.local:8768/"])
def test_plaintext_bridge_url_to_loopback_is_allowed(url):
    assert bridge_endpoint(_account("codex_oauth", oauth_bridge_url=url), "/x") == url.rstrip("/") + "/x"


def test_ca_file_is_forwarded_to_http_client(bridge_token):
    http = _HTTP(_ok({"authenticated": True, "rate_limits": [{"name": "p", "used_percent": 1}]}))

    CodexAdapter().collect(
        _account("codex_oauth", oauth_bridge_ca_file="/config/bridge-ca.pem"),
        CollectContext(http=http),
    )

    assert http.calls[0][2] == "/config/bridge-ca.pem"


def test_named_session_is_sent_as_query_parameter(bridge_token):
    http = _HTTP(_ok({"authenticated": True, "rate_limits": [{"name": "p", "used_percent": 1}]}))

    CodexAdapter().collect(_account("codex_oauth", oauth_session="work"), CollectContext(http=http))
    ClaudeAdapter().collect(_account("claude_code_oauth", oauth_session=""), CollectContext(http=http))

    assert http.calls[0][0] == BRIDGE + "/v1/codex/rate-limits?session=work"
    assert http.calls[1][0] == BRIDGE + "/v1/claude/usage"  # empty -> default, no query


@pytest.mark.parametrize("name", ["../x", "Work", "a b", "x" * 33, "-lead", "a?b=c"])
def test_invalid_session_name_is_config_error_not_request(bridge_token, name):
    http = _HTTP(_ok({}))

    snapshot = CodexAdapter().collect(_account("codex_oauth", oauth_session=name), CollectContext(http=http))

    assert snapshot.status is SnapshotStatus.ERROR
    assert "oauth_session" in snapshot.reason
    assert http.calls == []


def test_missing_credential_env_is_auth_error(monkeypatch):
    monkeypatch.delenv("BRIDGE_TOKEN", raising=False)
    http = _HTTP(_ok({}))

    snapshot = CodexAdapter().collect(_account("codex_oauth"), CollectContext(http=http))

    assert snapshot.status is SnapshotStatus.AUTH_ERROR
    assert http.calls == []


def test_bridge_401_is_auth_error(bridge_token):
    def handler(url, headers, ca_file):
        raise HttpAuthError("GET ... -> HTTP 401", status=401)

    snapshot = CodexAdapter().collect(_account("codex_oauth"), CollectContext(http=_HTTP(handler)))

    assert snapshot.status is SnapshotStatus.AUTH_ERROR


def test_bridge_reports_unauthenticated_oauth_session(bridge_token):
    http = _HTTP(_ok({"authenticated": False, "error": "oauth_session_invalid"}))

    snapshot = CodexAdapter().collect(_account("codex_oauth"), CollectContext(http=http))

    assert snapshot.status is SnapshotStatus.AUTH_ERROR
    assert "sign in again" in snapshot.reason


@pytest.mark.parametrize("body", [None, [], "string", {"rate_limits": "nope"}, {"rate_limits": []}, {"rate_limits": [1, "x", None]}])
def test_codex_malformed_payloads_are_unsupported_not_crashes(bridge_token, body):
    http = _HTTP(_ok(body))

    snapshot = CodexAdapter().collect(_account("codex_oauth"), CollectContext(http=http))

    assert snapshot.status is SnapshotStatus.UNSUPPORTED


@pytest.mark.parametrize("body", [None, [], {"metrics": "nope"}, {"metrics": {}}, {"metrics": {"input_tokens": "9"}}])
def test_claude_malformed_payloads_are_unsupported_not_crashes(bridge_token, body):
    http = _HTTP(_ok(body))

    snapshot = ClaudeAdapter().collect(_account("claude_code_oauth"), CollectContext(http=http))

    assert snapshot.status is SnapshotStatus.UNSUPPORTED


def test_non_finite_and_boolean_values_are_dropped(bridge_token):
    http = _HTTP(_ok({
        "authenticated": True,
        "rate_limits": [{"name": "p", "used_percent": float("nan"), "remaining_percent": float("inf"), "window_seconds": True}],
    }))

    snapshot = CodexAdapter().collect(_account("codex_oauth"), CollectContext(http=http))

    assert snapshot.status is SnapshotStatus.UNSUPPORTED


def test_rate_limit_names_are_normalized_and_deduplicated(bridge_token):
    http = _HTTP(_ok({
        "authenticated": True,
        "rate_limits": [
            {"name": "Primary.Window {{ x }}", "used_percent": 1},
            {"name": "primary window x", "used_percent": 2},
            {"name": None, "used_percent": 3},
            {"name": "", "used_percent": 4},
        ],
    }))

    snapshot = CodexAdapter().collect(_account("codex_oauth"), CollectContext(http=http))

    keys = [m.key for m in snapshot.metrics]
    assert keys == ["primary_window_x_used_percent", "window_used_percent"]
    assert all(k.replace("_", "").isalnum() for k in keys)


@pytest.mark.parametrize("raw,expected", [("primary", "primary"), ("Primary Window", "primary_window"), ("a.b{c}", "a_b_c"), (None, "window"), ("", "window"), ("___", "window")])
def test_metric_name(raw, expected):
    assert metric_name(raw) == expected


def test_safe_http_client_rejects_ca_file_for_plaintext_urls():
    client = SafeHttpClient(handler=lambda *a: HttpResponse(200, {}, {}))
    with pytest.raises(ValueError, match="https"):
        client.get("http://127.0.0.1/", ca_file="/tmp/ca.pem")
