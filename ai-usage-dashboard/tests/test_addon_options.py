import json

from ai_usage_dashboard.addon_options import (
    build_runtime_from_options_file,
    render_runtime_yaml,
    redacted_options_for_log,
)


def test_direct_mqtt_password_preserves_whitespace(tmp_path):
    options_path = tmp_path / "options.json"
    options_path.write_text(
        json.dumps(
            {
                "mqtt_host": "broker",
                "mqtt_username": "aiud",
                "mqtt_password_value": "  mqtt-password  ",
                "accounts": [
                    {
                        "provider": "muse_code",
                        "account_id": "local",
                        "credential_value": "configured",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    runtime, environment = build_runtime_from_options_file(str(options_path))

    assert runtime["mqtt"]["password_env"] == "AIUD_MQTT_PASSWORD_VALUE"
    assert environment["AIUD_MQTT_PASSWORD_VALUE"] == "  mqtt-password  "


def test_direct_account_credential_is_runtime_env_only(tmp_path):
    options_path = tmp_path / "options.json"
    options_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "provider": "muse_code",
                        "account_id": "local",
                        "credential_value": "account-secret",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    runtime, environment = build_runtime_from_options_file(str(options_path))
    rendered = render_runtime_yaml(runtime)

    assert runtime["accounts"][0].credential.env == "AIUD_ACCOUNT_CREDENTIAL_0"
    assert environment["AIUD_ACCOUNT_CREDENTIAL_0"] == "account-secret"
    assert "account-secret" not in rendered
    assert "account-secret" not in json.dumps(
        redacted_options_for_log(json.loads(options_path.read_text()))
    )


def test_direct_mqtt_password_env_name_is_reserved():
    from ai_usage_dashboard.addon_options import AddonOptionsError, options_to_runtime

    try:
        options_to_runtime(
            {
                "mqtt_username": "aiud",
                "mqtt_password_value": "mqtt-password",
                "accounts": [
                    {
                        "provider": "muse_code",
                        "account_id": "local",
                        "credential_env": "AIUD_MQTT_PASSWORD_VALUE",
                    }
                ],
            },
            environ={"AIUD_MQTT_PASSWORD_VALUE": "account-secret"},
            secrets={},
        )
    except AddonOptionsError as exc:
        assert "reserved" in str(exc)
    else:
        raise AssertionError("reserved MQTT password environment name was accepted")


def test_mqtt_password_env_account_namespace_is_reserved():
    from ai_usage_dashboard.addon_options import AddonOptionsError, options_to_runtime

    try:
        options_to_runtime(
            {
                "mqtt_username": "aiud",
                "mqtt_password_env": "AIUD_ACCOUNT_CREDENTIAL_0",
                "accounts": [
                    {
                        "provider": "muse_code",
                        "account_id": "local",
                        "credential_value": "account-secret",
                    }
                ],
            },
            environ={"AIUD_ACCOUNT_CREDENTIAL_0": "account-secret"},
            secrets={},
        )
    except AddonOptionsError as exc:
        assert "reserved" in str(exc)
    else:
        raise AssertionError("reserved MQTT password environment name was accepted")


def test_direct_account_credential_env_namespace_is_reserved():
    from ai_usage_dashboard.addon_options import AddonOptionsError, options_to_runtime

    try:
        options_to_runtime(
            {
                "accounts": [
                    {
                        "provider": "muse_code",
                        "account_id": "local",
                        "credential_env": "AIUD_ACCOUNT_CREDENTIAL_0",
                    }
                ]
            },
            environ={"AIUD_ACCOUNT_CREDENTIAL_0": "account-secret"},
            secrets={},
        )
    except AddonOptionsError as exc:
        assert "reserved" in str(exc)
    else:
        raise AssertionError("reserved account credential environment name was accepted")
