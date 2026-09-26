# AI Usage Dashboard (Home Assistant add-on)

Production deployment of the AI usage collector: a local collector polls AI
provider accounts and publishes Home Assistant MQTT discovery sensors plus
retained state. Architecture: **local collector -> MQTT discovery ->
Home Assistant dashboard.**

## Prerequisites

1. **Mosquitto broker add-on** (Settings > Add-ons > Mosquitto broker):
   install, create a broker user, and start it.
2. **MQTT integration** (Settings > Devices & services > Add integration >
   MQTT) pointing at the broker. The collector defaults to the internal
   broker hostname `core-mosquitto`, so no `homeassistant.local` setup is
   needed.

## Install (local add-on)

1. Copy this folder (`ai-usage-dashboard`) into the Supervisor `addons/`
   share on the Home Assistant host (via Samba, SSH, or the File editor),
   so it lands at `/addons/ai-usage-dashboard/`.
2. Go to Settings > Add-ons > Add-on Store, reload, and open
   **AI Usage Dashboard** under Local add-ons.
3. Install, then open the **Configuration** tab.

(Repository alternative: to publish these as a shareable add-on store,
place this folder at the root of a git repository so the Supervisor can
discover its `config.yaml`, and add that repository URL under Settings >
Add-ons > Add-on Store > Repositories.)

## Credentials (HA-safe, no raw secrets in config)

The add-on config holds only credential **names**. Secrets live in a
user-editable env file inside the add-on's own persistent config directory
(the `addon_config` mount; nothing else from Home Assistant is mapped):

1. Copy `secrets.env.example` to `secrets.env` inside the add-on config
   directory on the host: `addon_configs/ai_usage_dashboard/secrets.env`
   (via the File editor, Samba, or SSH add-on; owner-only permissions).
   Inside the container this file is mounted at `/config/secrets.env`,
   which is also the `secrets_file` default in the Configuration tab.
2. Fill in the values. Only the variables you reference are used.
3. In the Configuration tab, each account sets `credential_env` to the
   variable name, e.g. `OPENAI_API_KEY_PRIMARY`.
4. MQTT broker credentials work the same way: set `mqtt_username` and keep
   `mqtt_password_env: MQTT_PASSWORD` (or your own variable name) with the
   password in the secrets file.

Do not use `/data/...` for secrets: `/data` is the container-private
runtime area (generated config, state) and is not reachable from the
File editor / Samba. Keep raw values out of the Configuration tab.

macOS keychain is **not** used by the add-on. Raw secret values never
appear in logs, state topics, or discovery payloads; validation errors name
only the missing variable.

## Minimal configuration example

```yaml
mqtt_username: hass
mqtt_password_env: MQTT_PASSWORD
poll_interval: 900
accounts:
  - provider: openai
    account_id: primary
    display_name: OpenAI Primary
    credential_env: OPENAI_API_KEY_PRIMARY
    options:
      usage_url: https://api.openai.com/v1/organization/usage/completions
  - provider: openai
    account_id: secondary
    display_name: OpenAI Secondary
    credential_env: OPENAI_API_KEY_SECONDARY
```

A third OpenAI account is configuration-only: append another entry with a
new `account_id` (e.g. `tertiary`) and its own `credential_env`. No code
changes, and entity IDs stay distinct and stable
(`sensor.aiud_openai_tertiary_*`). The same holds for the other providers:
each `accounts:` entry sets `provider` to one of `openai`, `anthropic`,
`kimi`, `deepseek`, `opencode_go`, `muse_code`, `alibaba_coding_plan`, an
`account_id`, and a `credential_env` naming a variable in `secrets.env`,
plus provider `options` (`mode: local_stats` for OpenCode Go; no options
for Muse Code; `opt_in`/`cli_path`/`timeout` for Alibaba Coding Plan).

## Credentials per provider (names in Configuration, values in secrets.env)

Copy `secrets.env.example` to `secrets.env` and fill in only the variables
you reference. No browser automation or login prompts run in the add-on
container — obtain each credential outside (web login, `muse login`, or
browser devtools) and paste the value into the secrets file:

- **Grok (xAI)**: tracks API spend and request count via the management API.
  Requires a management API key (different from inference keys) and your
  team ID. Configure in the web console at `https://console.x.ai`, create
  a management key with billing read access, and store it as
  `XAI_MANAGEMENT_KEY`. Set `team_id` in the provider options.
- **OpenCode Go** (config-only): no quota endpoint is documented or
  polled — subscription usage lives in the official web console at
  `https://opencode.ai/auth`. Set `mode: local_stats`; the credential
  reference (e.g. `OPENCODE_GO_TOKEN`) only preserves the account
  registration. `opencode stats` reports local token/cost statistics
  (`local_session_usage`), never remaining subscription quota.
- **Alibaba Coding Plan** (explicitly opt-in CLI mode): make the official
  `bl` CLI available to the container (see "Provider CLIs" below), store
  the credential → e.g. `ALIBABA_CODING_PLAN_COOKIE`, and set
  `opt_in: true`, `cli_path: bl` (or the executable path), `timeout: 30`.
  The collector runs `bl usage coding-plan` and parses only the tested
  text shape. Warning: Alibaba intends Coding Plan keys for interactive
  coding tools and may prohibit automated scripts/backends — opt in only
  if that use is allowed for your plan.
- **Muse Code** (config-only): no subscription remaining-quota endpoint is
  documented or polled. The credential reference (e.g. `MUSE_CODE_TOKEN`)
  only preserves the account registration; a `META_API_KEY` value is
  pay-as-you-go API access, not proof of subscription quota. Manage the
  subscription in the official Meta/Muse web console.

```yaml
accounts:
  - provider: grok
    account_id: main
    display_name: Grok (xAI) Main
    credential_env: XAI_MANAGEMENT_KEY
    options:
      team_id: team_abc123
  - provider: opencode_go
    account_id: main
    display_name: OpenCode Go Main
    credential_env: OPENCODE_GO_TOKEN
    options:
      mode: local_stats
  - provider: muse_code
    account_id: main
    display_name: Muse Code Main
    credential_env: MUSE_CODE_TOKEN
  - provider: alibaba_coding_plan
    account_id: main
    display_name: Alibaba Coding Plan Main
    credential_env: ALIBABA_CODING_PLAN_COOKIE
    options:
      opt_in: true
      cli_path: bl
      timeout: 30
```

## Provider CLIs (Alibaba `bl`; reserved `opencode`/`muse` paths)

The default image does **not** bundle the `bl`, `opencode`, or `muse`
CLIs, and the collector never pretends otherwise: an Alibaba account
whose CLI is absent reports a clean `unsupported` status naming the
missing executable. To enable Alibaba polling, either mount the official
`bl` executable into the container and set `cli_path` to its path, or
extend the image with a pinned/bundled copy of the CLI:

```dockerfile
# Example only — pin a verified release in your own fork:
# COPY --from=<your-pinned-bl-image> /usr/local/bin/bl /usr/local/bin/bl
```

Credentials for the CLI come only from the secrets file via
`credential_env` (exposed solely inside the child process environment,
scrubbed from all output). No console RPC, inference-quota, or
undocumented endpoint is ever used.

## Dashboard

Interactive account management stays in each provider's own web console
(OpenCode dashboard, Alibaba Model Studio / Bailian console, Meta AI
account pages) — the add-on only reads quota snapshots. Import
`dashboard/lovelace.yaml` from the project repo via Settings >
Dashboards as below.

## Dashboard

Import `dashboard/lovelace.yaml` from the project repo via Settings >
Dashboards. Sensors appear as `sensor.aiud_<provider>_<account>_<metric>`
plus `_status` / `_reason` diagnostics and per-account availability.

## Persistence and polling

- State (last valid values) lives at `/data/state.json` and survives
  add-on restarts and rebuilds.
- `poll_interval` (60-86400 s, default 900) controls the loop.
- The entrypoint traps SIGTERM/SIGINT for graceful Supervisor stops.

## What is verified where

- **Verified offline** (no credentials, no broker): option-to-config
  conversion, redacted validation, discovery unique-ID stability,
  secret-leak scans, and fixture dry-runs (`tests/test_addon.py`).
- **Requires live HA**: broker connectivity, real provider quota values,
  and the imported dashboard rendering. Without credentials the collector
  reports `auth_error`/`error` per account and retries next interval.

## Troubleshooting

- Add-on stops immediately with `ERROR: ...`: the message names the bad
  option or missing variable (never its value). Fix Configuration or
  `/config/secrets.env` (host: `addon_configs/ai_usage_dashboard/secrets.env`)
  and restart.
- Sensors show `auth_error`: the referenced credential is wrong or lacks
  billing access; check the `_reason` sensor.
- Sensors show `unsupported`: no documented quota source exists for the
  account (OpenCode Go and Muse Code are always `unsupported` — check the
  `_reason` sensor for the official console link; Alibaba without
  `opt_in: true`, with a missing `bl` CLI, or with CLI output outside the
  tested text shape). Last valid values are never zeroed.
- Sensors show `error`: a bad option (removed legacy options such as
  `usage_url`/`key_url`/`auth_mode`/`mode`/`region`/`api_url`, a bad
  `cli_path`/`timeout`) or a failing `bl` invocation (timeout, nonzero
  exit); the `_reason` sensor names the problem without ever showing the
  secret.
