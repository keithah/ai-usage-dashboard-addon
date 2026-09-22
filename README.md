# AI Usage Dashboard Home Assistant app

Public Home Assistant app repository for polling documented AI-provider usage and quota data, then publishing MQTT discovery sensors for dashboards.

Repository: <https://github.com/keithah/ai-usage-dashboard-addon>

## Install from Home Assistant

1. Open **Settings → Apps → App store**.
2. Open the repository menu, choose **Repositories**, and add:
   `https://github.com/keithah/ai-usage-dashboard-addon`
3. Refresh the app store and select **AI Usage Dashboard**.
4. Install it, open **Configuration**, and set the MQTT and account options.
5. Start the app and verify the generated MQTT sensors in Home Assistant.

The app requires a working MQTT broker and the Home Assistant MQTT integration. The app does not include provider credentials and does not prompt for browser logins.

## Credentials

Keep secret values in the add-on's persistent configuration file, not in GitHub or the Home Assistant app options:

1. Copy `ai-usage-dashboard/secrets.env.example` to the app's persistent config directory as `secrets.env`.
2. Fill in only the variables referenced by your configured accounts.
3. Set each account's `credential_env` to the variable name, for example
   `OPENAI_API_KEY_PRIMARY`.
4. Set `mqtt_password_env` to the variable containing the MQTT password.

The default path inside the app is `/config/secrets.env`. Never commit a populated `secrets.env`, API keys, cookies, tokens, or private keys.

## Configuration example

```yaml
mqtt_host: core-mosquitto
mqtt_port: 1883
mqtt_username: hass
mqtt_password_env: MQTT_PASSWORD
poll_interval: 900
accounts:
  - provider: openai
    account_id: primary
    display_name: OpenAI Primary
    credential_env: OPENAI_API_KEY_PRIMARY
```

Supported provider behavior and provider-specific options are documented in [`ai-usage-dashboard/README.md`](ai-usage-dashboard/README.md).

## Repository layout

- `repository.yaml` — Home Assistant repository metadata.
- `ai-usage-dashboard/` — app metadata, image build files, runtime package, and safe example configuration.
- `.github/workflows/lint.yaml` — Home Assistant app lint workflow.

## Security status

This repository is public by design. Runtime secrets are supplied through Home Assistant's persistent app configuration and are excluded from the repository. Current repository content and Git history must remain free of populated secret files.
