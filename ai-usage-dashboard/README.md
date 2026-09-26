# AI Usage Dashboard

Home Assistant add-on that tracks AI provider usage, costs, and review metrics across multiple services. Polls provider APIs on a configurable interval and publishes MQTT discovery sensors for real-time dashboarding.

## Overview

This add-on collects usage data from AI coding assistants and services, normalizes it into a consistent metric format, and exposes it as Home Assistant sensors via MQTT discovery. Each provider account becomes a set of sensors tracking costs, token usage, review metrics, and account status.

**Key features:**
- Multi-provider support with provider-specific API integrations
- Secure credential management via secrets file (never stored in config)
- Automatic MQTT discovery - sensors appear immediately in Home Assistant
- State persistence across restarts
- Redacted error reporting (secrets never appear in logs or sensor data)
- Configurable polling interval (60-86400 seconds)

## Supported Providers

### OpenAI
**What it tracks:** Organization-level usage and subscription data
- Monthly spend (USD)
- Usage metrics from `/v1/organization/usage` endpoint
- Subscription tier and limits from `/v1/organization/subscription`

**API endpoints:**
- `https://api.openai.com/v1/organization/usage` (usage data)
- `https://api.openai.com/v1/organization/subscription` (subscription info)

**Authentication:** Bearer token (API key with organization read access)

**Metrics created:**
- `monthly_spend_usd` - Current month spend in USD
- `usage_requests` - Number of API requests
- `usage_tokens` - Total tokens consumed
- `subscription_tier` - Current subscription level
- `subscription_limit` - Usage limit for current tier

### Anthropic
**What it tracks:** API usage and billing data
- Monthly spend (USD)
- Token usage (input/output tokens)
- Request counts

**API endpoints:**
- `https://api.anthropic.com/v1/usage` (usage data)
- `https://api.anthropic.com/v1/billing` (billing data)

**Authentication:** Bearer token (API key)

**Metrics created:**
- `monthly_spend_usd` - Current month spend in USD
- `input_tokens` - Input tokens consumed
- `output_tokens` - Output tokens consumed
- `total_requests` - Total API requests

### Grok (xAI)
**What it tracks:** Team-level API usage and costs
- Monthly spend (USD)
- Token usage
- Request counts per team

**API endpoints:**
- `https://api.x.ai/v1/billing/teams/{team_id}/usage` (team usage)

**Authentication:** Bearer token (management API key)

**Configuration:** Requires `team_id` in options

**Metrics created:**
- `monthly_spend_usd` - Current month spend in USD
- `input_tokens` - Input tokens consumed
- `output_tokens` - Output tokens consumed
- `total_requests` - Total API requests

### OpenRouter
**What it tracks:** Account-level credits and usage across all models
- Total credits purchased (USD)
- Credits used (USD)
- Credits remaining (USD)

**API endpoints:**
- `https://openrouter.ai/api/v1/credits` (account credits)

**Authentication:** Bearer token (management API key)

**Metrics created:**
- `credits_purchased_usd` - Total credits purchased
- `credits_used_usd` - Credits consumed
- `credits_remaining_usd` - Remaining credit balance

### Gemini (Google)
**What it tracks:** Current-month Gemini-related spend from Google Cloud Billing
- Monthly spend (USD or CNY, based on billing currency)
- Billing row count

**Data source:** Google Cloud BigQuery billing export (not direct API)

**Authentication:** OAuth2 JWT with service account credentials
- Requires BigQuery Job User and BigQuery Data Viewer roles
- Service account JSON key stored in secrets file

**Configuration:** Requires `project_id` and `billing_export_table` in options

**Metrics created:**
- `monthly_cost` - Current month spend in billing currency
- `billing_rows` - Number of billing records

**Implementation notes:**
- Uses BigQuery export because Google Cloud Billing REST API only exposes catalog/pricing data, not accrued spend
- Query filters for Gemini, Generative Language, and Vertex AI services
- Supports USD and CNY currencies; other currencies return UNSUPPORTED status
- Empty billing data returns zero-cost metrics (not UNSUPPORTED)
- Mixed-currency results return ERROR status

### CodeRabbit
**What it tracks:** Code review metrics and activity
- Total reviews
- Average complexity score
- Estimated review time (seconds)
- Comments posted
- Comments accepted

**API endpoints:**
- `https://api.coderabbit.ai/v1/metrics` (review metrics)

**Authentication:** `x-coderabbitai-api-key` header (API key)

**Configuration:** Requires `organization_id` or `org_id` in options

**Metrics created:**
- `total_reviews` - Number of code reviews
- `average_complexity` - Mean complexity score
- `estimated_review_seconds` - Estimated review time
- `comments_posted` - Total comments posted
- `comments_accepted` - Comments accepted by users

**Implementation notes:**
- Supports pagination (up to 100 pages)
- Validates date ranges and cursor consistency
- Repeated cursors return ERROR status
- Window metadata includes start/end dates

### Kimi (Moonshot AI)
**What it tracks:** API usage and costs
- Monthly spend (CNY)
- Token usage
- Request counts

**API endpoints:**
- `https://api.moonshot.cn/v1/usage` (usage data)

**Authentication:** Bearer token (API key)

**Metrics created:**
- `monthly_spend_cny` - Current month spend in CNY
- `input_tokens` - Input tokens consumed
- `output_tokens` - Output tokens consumed
- `total_requests` - Total API requests

### DeepSeek
**What it tracks:** API usage and costs
- Monthly spend (CNY)
- Token usage
- Request counts

**API endpoints:**
- `https://api.deepseek.com/v1/usage` (usage data)

**Authentication:** Bearer token (API key)

**Metrics created:**
- `monthly_spend_cny` - Current month spend in CNY
- `input_tokens` - Input tokens consumed
- `output_tokens` - Output tokens consumed
- `total_requests` - Total API requests

### OpenCode Go
**What it tracks:** Local session statistics only (no API polling)
- Local token usage
- Local cost estimates

**Data source:** Local `opencode stats` command output

**Authentication:** Token reference only (no API calls)

**Configuration:** Requires `mode: local_stats` in options

**Metrics created:**
- `local_tokens` - Tokens used in local sessions
- `local_cost_usd` - Estimated local session cost

**Implementation notes:**
- No public API for subscription quota
- Tracks local session data only
- Subscription management must be done via web console

### Muse Code
**What it tracks:** Configuration reference only (no API polling)
- Account registration reference

**Data source:** None (config-only)

**Authentication:** Token reference only (no API calls)

**Metrics created:** None

**Implementation notes:**
- No public API for subscription quota
- Provider exists only to track account configuration
- Subscription management must be done via web console

### Alibaba Coding Plan
**What it tracks:** Coding plan subscription quota via CLI
- Remaining quota
- Usage statistics

**Data source:** `bl usage coding-plan` CLI command

**Authentication:** Cookie-based (stored in secrets file)

**Configuration:** Requires `opt_in: true`, `cli_path`, and `timeout` in options

**Metrics created:**
- `remaining_quota` - Remaining usage quota
- `usage_percentage` - Percentage of quota used

**Implementation notes:**
- Requires `bl` CLI to be installed and available in container
- CLI must be mounted or bundled in custom image
- Without CLI, returns UNSUPPORTED status
- Alibaba may prohibit automated access to Coding Plan keys

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Home Assistant Add-on Container                            │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  AI Usage Dashboard Collector                         │  │
│  │  - Polls provider APIs on interval                    │  │
│  │  - Normalizes metrics                                 │  │
│  │  - Publishes to MQTT                                  │  │
│  └───────────────────────────────────────────────────────┘  │
│                          │                                   │
│                          ▼                                   │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  MQTT Client (paho-mqtt)                              │  │
│  │  - Connects to Mosquitto broker                       │  │
│  │  - Publishes discovery messages                       │  │
│  │  - Publishes state updates                            │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  Mosquitto MQTT Broker (Home Assistant Add-on)              │
│  - Receives discovery messages                              │
│  - Receives state updates                                   │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  Home Assistant                                             │
│  - MQTT Integration discovers sensors                       │
│  - Sensors appear in UI                                     │
│  - Dashboard displays metrics                               │
└─────────────────────────────────────────────────────────────┘
```

## Sensor Naming Convention

All sensors follow the pattern: `sensor.aiud_<provider>_<account_id>_<metric>`

**Examples:**
- `sensor.aiud_openai_primary_monthly_spend_usd`
- `sensor.aiud_anthropic_work_input_tokens`
- `sensor.aiud_coderabbit_main_total_reviews`

**Diagnostic sensors:**
- `sensor.aiud_<provider>_<account_id>_status` - Account status (fresh/stale/auth_error/unsupported/error)
- `sensor.aiud_<provider>_<account_id>_reason` - Human-readable status reason

## Status Values

- **fresh** - Data retrieved successfully in current poll cycle
- **stale** - Transient failure; showing last valid values
- **auth_error** - Credential missing, invalid, or lacks required permissions
- **unsupported** - No documented API for this metric, or provider requires additional setup
- **error** - Non-transient failure (malformed response, configuration error)

## Security Model

### Credential Storage
- Credentials stored in `/config/secrets.env` (mapped from `addon_configs/ai_usage_dashboard/secrets.env`)
- Configuration references only variable names, never values
- Secrets file permissions: 0600 (owner read/write only)
- Raw secrets never appear in:
  - Configuration files
  - Logs
  - MQTT messages
  - Sensor attributes
  - Error messages

### Credential Resolution
1. Add-on reads `credential_env` from configuration
2. Looks up variable name in secrets file
3. Passes value to provider adapter
4. Adapter uses value for API authentication
5. Value is never logged or exposed in sensor data

### Error Handling
- Validation errors name the missing variable, never its value
- HTTP errors redact authorization headers
- JSON parsing errors do not expose credential content
- All error messages are safe to display in UI

## Configuration Reference

### Global Options

```yaml
mqtt_host: "core-mosquitto"  # MQTT broker hostname
mqtt_port: 1883              # MQTT broker port
mqtt_username: ""            # MQTT username (optional)
mqtt_password_env: "MQTT_PASSWORD"  # Env var name for MQTT password
secrets_file: "/config/secrets.env"  # Path to secrets file
poll_interval: 900           # Polling interval in seconds (60-86400)
discovery_prefix: "homeassistant"  # MQTT discovery prefix
data_dir: "/data"            # Runtime data directory
```

### Account Configuration

Each account requires:
- `provider` - Provider name (openai, anthropic, etc.)
- `account_id` - Unique identifier for this account
- `display_name` - Human-readable name (optional)
- `credential_env` - Environment variable name in secrets file
- `options` - Provider-specific configuration (JSON object)

### Provider-Specific Options

**OpenAI:**
```yaml
options:
  usage_url: "https://api.openai.com/v1/organization/usage"
  subscription_url: "https://api.openai.com/v1/organization/subscription"
```

**Grok:**
```yaml
options:
  team_id: "team_abc123"
```

**OpenRouter:**
```yaml
options:
  credits_url: "https://openrouter.ai/api/v1/credits"
```

**Gemini:**
```yaml
options:
  project_id: "my-project-id"
  billing_export_table: "my-project.billing_dataset.gcp_billing_export_v1_XXXXXX"
  currency: "USD"  # Optional, defaults to USD
```

**CodeRabbit:**
```yaml
options:
  organization_id: "org_123"  # or org_id
  days: 30  # Lookback window
  limit: 1000  # Results per page
  start_date: "2026-01-01"  # Optional, overrides days
  end_date: "2026-01-31"  # Optional, defaults to today
```

**OpenCode Go:**
```yaml
options:
  mode: "local_stats"
```

**Alibaba Coding Plan:**
```yaml
options:
  opt_in: true
  cli_path: "/usr/local/bin/bl"
  timeout: 30
```

## State Persistence

- State stored in `/data/state.json`
- Survives add-on restarts and rebuilds
- Preserves last valid metric values
- Used to provide stale data during transient failures
- Automatically updated after each successful poll

## Polling Behavior

- Default interval: 900 seconds (15 minutes)
- Range: 60-86400 seconds
- Each provider polled independently
- Failed polls do not block other providers
- Transient errors (429, 5xx, timeouts) trigger retry with backoff
- Last valid values preserved during transient failures

## Troubleshooting

### Add-on Won't Start

**Symptom:** Add-on stops immediately with `ERROR: ...`

**Cause:** Configuration validation failure

**Solution:**
1. Check add-on logs for error message
2. Error names the problematic option or missing variable
3. Fix configuration or add missing variable to secrets file
4. Restart add-on

### Sensors Show auth_error

**Symptom:** Sensor status is `auth_error`

**Cause:** Credential missing, invalid, or lacks required permissions

**Solution:**
1. Check `_reason` sensor for details
2. Verify variable exists in secrets file
3. Verify credential has required permissions
4. For Gemini: verify service account has BigQuery roles
5. For CodeRabbit: verify API key is valid

### Sensors Show unsupported

**Symptom:** Sensor status is `unsupported`

**Cause:** Provider requires additional setup or has no public API

**Solution:**
1. Check `_reason` sensor for details
2. For Alibaba: install `bl` CLI and set `opt_in: true`
3. For OpenCode Go / Muse Code: no API available, config-only
4. For Gemini: verify billing export is enabled

### Sensors Show error

**Symptom:** Sensor status is `error`

**Cause:** Non-transient failure (malformed response, configuration error)

**Solution:**
1. Check `_reason` sensor for details
2. Verify provider options are correct
3. For Gemini: verify project_id and billing_export_table format
4. For CodeRabbit: verify date format (YYYY-MM-DD)

### No Sensors Appear

**Symptom:** Add-on running but no sensors in Home Assistant

**Cause:** MQTT discovery not working

**Solution:**
1. Verify Mosquitto broker is running
2. Verify MQTT integration is configured
3. Check add-on logs for MQTT connection errors
4. Verify `discovery_prefix` matches MQTT integration setting
5. Restart MQTT integration

### Stale Data

**Symptom:** Sensors show old data

**Cause:** Polling failures or interval too long

**Solution:**
1. Check `_status` sensor (should be `fresh`)
2. Check `_reason` sensor for error details
3. Reduce `poll_interval` if too long
4. Check network connectivity to provider APIs

## Development

### Repository Structure

```
ai-usage-dashboard/
├── config.yaml              # Add-on configuration
├── Dockerfile               # Container build instructions
├── README.md                # This file
├── run.sh                   # Container entrypoint
├── translations/
│   └── en.yaml             # UI translations
└── rootfs/
    └── app/
        └── ai_usage_dashboard/
            ├── __init__.py
            ├── addon_options.py    # Configuration validation
            ├── collector.py        # Main collection loop
            ├── credentials.py      # Credential resolution
            ├── discovery.py        # MQTT discovery
            ├── http_client.py      # HTTP client with retry
            ├── models.py           # Data models
            └── providers/          # Provider adapters
                ├── __init__.py
                ├── base.py
                ├── _helpers.py
                ├── openai.py
                ├── anthropic.py
                ├── grok.py
                ├── openrouter.py
                ├── gemini.py
                ├── coderabbit.py
                ├── kimi.py
                ├── deepseek.py
                ├── opencode_go.py
                ├── muse_code.py
                └── alibaba_coding_plan.py
```

### Adding a New Provider

1. Create provider adapter in `providers/<name>.py`
2. Implement `Adapter` class with `collect()` method
3. Return `AccountSnapshot` with metrics
4. Register provider in `providers/__init__.py`
5. Add to `SUPPORTED_PROVIDERS` in `config.py`
6. Update `translations/en.yaml` with setup instructions
7. Update this README with provider documentation

### Testing

```bash
# Run tests
pytest ai-usage-dashboard/tests/

# Run CodeRabbit review
coderabbit review --uncommitted --include-untracked --agent

# Compile check
python3 -m compileall ai-usage-dashboard/rootfs/app
```

## License

This project is provided as-is for use with Home Assistant.

## Support

For issues, feature requests, or questions:
1. Check troubleshooting section above
2. Review add-on logs
3. Check `_status` and `_reason` sensors
4. Open an issue on GitHub
