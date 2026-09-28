import sys
import types

import pytest

from ai_usage_dashboard.models import AccountSnapshot, Metric, SnapshotStatus, Unit
from ai_usage_dashboard.mqtt import (
    AVAILABILITY_OFFLINE,
    _publish_and_wait,
    publish_live,
)


class _PublishInfo:
    def __init__(self, published=True, on_wait=None):
        self.wait_timeout = None
        self.published = published
        self.on_wait = on_wait

    def wait_for_publish(self, timeout=None):
        self.wait_timeout = timeout
        if self.on_wait:
            self.on_wait()

    def is_published(self):
        return self.published


class _Client:
    def __init__(self):
        self.info = _PublishInfo()

    def publish(self, topic, payload, qos=0, retain=False):
        self.topic = topic
        self.payload = payload
        self.qos = qos
        self.retain = retain
        return self.info


def test_publish_and_wait_flushes_message_before_disconnect():
    client = _Client()

    _publish_and_wait(client, "homeassistant/sensor/example/config", "{}", retain=True)

    assert client.topic == "homeassistant/sensor/example/config"
    assert client.qos == 1
    assert client.retain is True
    assert client.info.wait_timeout == 10


class _LiveClient:
    def __init__(self, fail_config=False):
        self.fail_config = fail_config
        self.events = []

    def connect(self, host, port):
        self.events.append(("connect", host, port))

    def loop_start(self):
        self.events.append(("loop_start",))

    def loop_stop(self):
        self.events.append(("loop_stop",))

    def disconnect(self):
        self.events.append(("disconnect",))

    def publish(self, topic, payload, qos=0, retain=False):
        self.events.append(("publish", topic, payload, qos, retain))
        return _PublishInfo(
            published=not (self.fail_config and topic.endswith("/config")),
            on_wait=lambda: self.events.append(("ack", topic)),
        )


def _snapshot():
    return AccountSnapshot(
        provider="openai",
        account_id="primary",
        display_name="OpenAI Primary",
        status=SnapshotStatus.FRESH,
        metrics=[Metric("requests", "Requests", 3, Unit.REQUESTS)],
    )


def _install_fake_mqtt(monkeypatch, client):
    client_module = types.ModuleType("paho.mqtt.client")
    client_module.Client = lambda: client
    mqtt_module = types.ModuleType("paho.mqtt")
    mqtt_module.client = client_module
    paho_module = types.ModuleType("paho")
    paho_module.mqtt = mqtt_module
    monkeypatch.setitem(sys.modules, "paho", paho_module)
    monkeypatch.setitem(sys.modules, "paho.mqtt", mqtt_module)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", client_module)


def test_publish_live_marks_online_after_discovery(monkeypatch):
    client = _LiveClient()
    _install_fake_mqtt(monkeypatch, client)

    result = publish_live([_snapshot()], host="broker")

    topics = [event[1] for event in client.events if event[0] == "publish"]
    assert topics[-1].endswith("/availability")
    online_index = next(
        index
        for index, event in enumerate(client.events)
        if event[0] == "publish" and event[1].endswith("/availability")
    )
    published_before_online = [
        event[1]
        for index, event in enumerate(client.events)
        if index < online_index and event[0] == "publish"
    ]
    acknowledged_before_online = [
        event[1]
        for index, event in enumerate(client.events)
        if index < online_index and event[0] == "ack"
    ]
    assert acknowledged_before_online == published_before_online
    assert result["published"] == len(topics)
    assert client.events[-1] == ("disconnect",)


def test_publish_live_marks_offline_when_discovery_fails(monkeypatch):
    client = _LiveClient(fail_config=True)
    _install_fake_mqtt(monkeypatch, client)

    with pytest.raises(RuntimeError, match="did not complete"):
        publish_live([_snapshot()], host="broker")

    topics = [event[1] for event in client.events if event[0] == "publish"]
    assert topics[-1].endswith("/availability")
    assert client.events[-1] == ("disconnect",)
    availability_payloads = [
        event[2]
        for event in client.events
        if event[0] == "publish" and event[1].endswith("/availability")
    ]
    assert availability_payloads[-1] == AVAILABILITY_OFFLINE
    assert "online" not in availability_payloads


# --- secret-key guard vs. token *counters* -----------------------------------


def _snap(metrics):
    from ai_usage_dashboard.models import Window

    return AccountSnapshot(
        provider="claude_code_oauth",
        account_id="personal",
        display_name="Claude",
        status=SnapshotStatus.FRESH,
        reason="",
        fetched_at="2026-09-28T00:00:00Z",
        metrics=[Metric(k, k, v, Unit.TOKENS, Window(kind="calendar_month", label="m")) for k, v in metrics],
    )


def test_state_payload_allows_numeric_token_counters():
    """Regression: 0.2.0 crashed publish with 'payload key looks like a secret: input_tokens'."""
    from ai_usage_dashboard.mqtt import state_payload

    payload = state_payload(_snap([("input_tokens", 100), ("cache_read_input_tokens", 5), ("total_tokens", 105)]))
    assert payload["metrics"] == {"input_tokens": 100, "cache_read_input_tokens": 5, "total_tokens": 105}


@pytest.mark.parametrize(
    "payload",
    [
        {"metrics": {"access_token": 1}},              # not the *_tokens counter shape
        {"metrics": {"tokens_secret": 1}},
        {"metrics": {"input_tokens": "eyJhbGciOi..."}},  # counter name but string value
        {"metrics": {"input_tokens": True}},
        {"metrics": {"token": 5}},                       # singular
        {"refresh_token": "x"},
        {"metrics": {"bearer_tokens": {"nested": "v"}}},  # counter name, non-numeric value
    ],
)
def test_state_payload_still_rejects_secret_shaped_keys(payload):
    from ai_usage_dashboard.mqtt import assert_no_secrets

    with pytest.raises(ValueError, match="looks like a secret"):
        assert_no_secrets(payload)
