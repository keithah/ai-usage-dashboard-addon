"""MQTT publisher with Home Assistant discovery and retained state topics.

The paho-mqtt dependency is optional: dry-run JSON output (used by offline
tests and `publish --once --dry-run`) needs no broker and no extra package.
Discovery/state payloads never contain secrets or credential references.
"""
from __future__ import annotations

import json
import re

from .models import AccountSnapshot

NODE_ID = "ai_usage_dashboard"
DISCOVERY_PREFIX = "homeassistant"
AVAILABILITY_ONLINE = "online"
AVAILABILITY_OFFLINE = "offline"

_SECRET_KEY_HINTS = ("api_key", "apikey", "token", "secret", "password", "bearer", "credential")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")


def unique_id(snapshot: AccountSnapshot, metric_key: str) -> str:
    return f"aiud_{slug(snapshot.provider)}_{slug(snapshot.account_id)}_{slug(metric_key)}"


def state_topic(snapshot: AccountSnapshot) -> str:
    return f"{NODE_ID}/{slug(snapshot.provider)}/{slug(snapshot.account_id)}/state"


def availability_topic(snapshot: AccountSnapshot) -> str:
    return f"{NODE_ID}/{slug(snapshot.provider)}/{slug(snapshot.account_id)}/availability"


def discovery_topic(sensor: str, *, prefix: str = DISCOVERY_PREFIX) -> str:
    return f"{prefix}/sensor/{sensor}/config"


def device_info(snapshot: AccountSnapshot) -> dict:
    return {
        "identifiers": [f"aiud_{slug(snapshot.provider)}_{slug(snapshot.account_id)}"],
        "name": f"{snapshot.display_name} ({snapshot.provider})",
        "manufacturer": snapshot.provider,
        "model": "AI account usage",
    }


def state_payload(snapshot: AccountSnapshot) -> dict:
    payload = {
        "provider": snapshot.provider,
        "account_id": snapshot.account_id,
        "display_name": snapshot.display_name,
        "status": snapshot.status.value,
        "reason": snapshot.reason,
        "fetched_at": snapshot.fetched_at,
        "metrics": {m.key: m.value for m in snapshot.metrics},
        "metric_units": {m.key: m.unit.value for m in snapshot.metrics},
    }
    assert_no_secrets(payload)
    return payload


def discovery_payloads(
    snapshot: AccountSnapshot, *, prefix: str = DISCOVERY_PREFIX
) -> list[tuple[str, dict]]:
    """Build (topic, payload) discovery configs: one sensor per metric plus
    status and reason diagnostic sensors.

    Every entity carries an availability config pointing at the per-account
    availability topic; the live publisher retains "online" there each run.
    """
    topic = state_topic(snapshot)
    availability = {
        "topic": availability_topic(snapshot),
        "payload_available": AVAILABILITY_ONLINE,
        "payload_not_available": AVAILABILITY_OFFLINE,
    }
    device = device_info(snapshot)
    out: list[tuple[str, dict]] = []
    for metric in snapshot.metrics:
        uid = unique_id(snapshot, metric.key)
        out.append(
            (
                discovery_topic(uid, prefix=prefix),
                {
                    "name": f"{snapshot.display_name} {metric.label}",
                    "object_id": uid,
                    "unique_id": uid,
                    "state_topic": topic,
                    "value_template": "{{ value_json.metrics." + metric.key + " }}",
                    "device": device,
                    "json_attributes_topic": topic,
                    "availability": dict(availability),
                },
            )
        )
    for key, label in (("status", "Status"), ("reason", "Reason")):
        uid = unique_id(snapshot, key)
        out.append(
            (
                discovery_topic(uid, prefix=prefix),
                {
                    "name": f"{snapshot.display_name} {label}",
                    "object_id": uid,
                    "unique_id": uid,
                    "state_topic": topic,
                    "value_template": "{{ value_json." + key + " }}",
                    "device": device,
                    "entity_category": "diagnostic",
                    "json_attributes_topic": topic,
                    "availability": dict(availability),
                },
            )
        )
    for _, payload in out:
        assert_no_secrets(payload)
    return out


def build_dry_run(
    snapshots: list[AccountSnapshot], *, discovery_prefix: str = DISCOVERY_PREFIX
) -> dict:
    states = []
    discoveries = []
    for snap in snapshots:
        states.append({"topic": state_topic(snap), "payload": state_payload(snap)})
        for topic, payload in discovery_payloads(snap, prefix=discovery_prefix):
            discoveries.append({"topic": topic, "payload": payload})
    return {"states": states, "discovery": discoveries}


def assert_no_secrets(payload: object) -> None:
    """Fail if any key/value in a payload looks like a secret or reference."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).lower()
            if any(hint in lowered for hint in _SECRET_KEY_HINTS):
                raise ValueError(f"payload key looks like a secret: {key!r}")
            assert_no_secrets(value)
    elif isinstance(payload, list):
        for item in payload:
            assert_no_secrets(item)


def publish_live(
    snapshots: list[AccountSnapshot],
    *,
    host: str,
    port: int = 1883,
    username: str | None = None,
    password: str | None = None,
    retain: bool = True,
    discovery_prefix: str = DISCOVERY_PREFIX,
) -> dict:
    try:
        import paho.mqtt.client as mqtt  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "paho-mqtt is not installed; install the 'mqtt' extra or use --dry-run"
        ) from exc
    client = mqtt.Client()
    if username:
        client.username_pw_set(username, password)
    client.connect(host, port)
    client.loop_start()
    published = 0
    try:
        for snap in snapshots:
            client.publish(
                availability_topic(snap), AVAILABILITY_ONLINE, retain=True
            )
            published += 1
            client.publish(state_topic(snap), json.dumps(state_payload(snap)), retain=retain)
            published += 1
            for topic, payload in discovery_payloads(snap, prefix=discovery_prefix):
                client.publish(topic, json.dumps(payload), retain=retain)
                published += 1
    finally:
        client.loop_stop()
        client.disconnect()
    return {"published": published, "host": host, "port": port}
