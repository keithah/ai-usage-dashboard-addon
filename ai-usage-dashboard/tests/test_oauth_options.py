from ai_usage_dashboard.addon_options import options_to_runtime


def test_oauth_account_uses_bridge_url_and_bridge_token_reference():
    runtime = options_to_runtime(
        {
            "accounts": [
                {
                    "provider": "codex_oauth",
                    "account_id": "personal",
                    "credential_env": "AIUD_OAUTH_BRIDGE_TOKEN",
                    "oauth_bridge_url": "https://192.168.42.10:8768",
                }
            ]
        },
        environ={"AIUD_OAUTH_BRIDGE_TOKEN": "bridge-token"},
        secrets={},
    )

    account = runtime["accounts"][0]
    assert account.provider == "codex_oauth"
    assert account.credential.env == "AIUD_OAUTH_BRIDGE_TOKEN"
    assert account.options["oauth_bridge_url"].endswith(":8768")
