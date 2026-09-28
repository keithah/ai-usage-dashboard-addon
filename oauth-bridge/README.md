# AI Usage Dashboard OAuth bridge

This bridge runs on the Mac that owns the authenticated Codex and Claude Code CLI sessions. The Home Assistant add-on calls it for normalized metrics; provider OAuth files and tokens remain on the Mac. It exposes only `/health`, `/v1/sessions`, `/v1/codex/rate-limits`, and `/v1/claude/usage`, never proxies arbitrary URLs or commands, and never accepts or stores an API key.

## Transport security (required)

The add-on authenticates to the bridge with a bearer token. That token must not cross the LAN in plaintext, so:

- The bridge **refuses to start** on a non-loopback address (`0.0.0.0`, a LAN IP) unless `--tls-cert`/`--tls-key` are supplied.
- The add-on **refuses** an `http://` `oauth_bridge_url` unless it points at `localhost`/`127.0.0.1`.
- The token file and TLS private key must be regular files with mode `600` owned by the bridge user; the bridge exits otherwise.

## Install on the Mac

From the repository checkout:

```sh
mkdir -p "$HOME/.config/aiud"
python3 -c 'import secrets; from pathlib import Path; p=Path.home()/".config/aiud/bridge-token"; p.write_text(secrets.token_hex(32)+"\n"); p.chmod(0o600)'

# Self-signed certificate bound to the Mac's LAN IP (replace 192.168.42.176).
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout "$HOME/.config/aiud/bridge-key.pem" \
  -out "$HOME/.config/aiud/bridge-cert.pem" \
  -subj "/CN=aiud-oauth-bridge" \
  -addext "subjectAltName=IP:192.168.42.176" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign" \
  -addext "extendedKeyUsage=serverAuth"
chmod 600 "$HOME/.config/aiud/bridge-key.pem"
```

Copy `com.keithah.aiud-oauth-bridge.plist` to `~/Library/LaunchAgents/`, replacing `REPLACE_WITH_REPOSITORY` with the absolute repository path and `REPLACE_WITH_HOME` with your home directory. Then load it:

```sh
launchctl bootout "gui/$(id -u)/com.keithah.aiud-oauth-bridge" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.keithah.aiud-oauth-bridge.plist"
curl -fsS --cacert "$HOME/.config/aiud/bridge-cert.pem" https://192.168.42.176:8768/health
```

Do not port-forward the bridge or expose it to the internet.

## Home Assistant side

1. Copy the **certificate** (not the key) into the add-on config folder so the add-on can verify the bridge:
   `~/.config/aiud/bridge-cert.pem` -> `/addon_configs/<slug>_ai_usage_dashboard/bridge-ca.pem` (mounted as `/config/bridge-ca.pem` inside the add-on).
2. Put the bridge token value in the add-on's `/config/secrets.env` as `AIUD_OAUTH_BRIDGE_TOKEN`. Do not commit it or paste it into GitHub.
3. Add account rows. This is a bridge access token reference, not an OpenAI or Anthropic API key.

```yaml
- provider: codex_oauth
  account_id: personal
  display_name: Codex OAuth
  credential_env: AIUD_OAUTH_BRIDGE_TOKEN
  oauth_bridge_url: https://192.168.42.176:8768
  oauth_bridge_ca_file: /config/bridge-ca.pem

- provider: claude_code_oauth
  account_id: personal
  display_name: Claude Code OAuth
  credential_env: AIUD_OAUTH_BRIDGE_TOKEN
  oauth_bridge_url: https://192.168.42.176:8768
  oauth_bridge_ca_file: /config/bridge-ca.pem
```

If you use a certificate signed by a CA that Home Assistant already trusts, omit `oauth_bridge_ca_file`. Certificate validation and hostname/IP verification are never disabled.

## Several Codex or Claude accounts

Each CLI can only be signed in to one account at a time, so the bridge keeps a **captured copy of the sign-in** per named session. Sign in, capture, sign out, sign in to the next account, capture again:

```sh
codex login && claude login            # account A
python3 oauth-bridge/bridge.py capture personal
codex logout && claude logout
codex login && claude login            # account B
python3 oauth-bridge/bridge.py capture work
```

`capture` copies only the credential file each CLI needs (`auth.json` for Codex, `.credentials.json` for Claude Code) into `~/.config/aiud/sessions/<name>/{codex,claude}/` with mode `600`; transcripts, caches, and memories are not copied. Use `--only codex` or `--only claude` to capture just one tool. Re-running `capture` for the same name replaces that session's credentials. The session name must match `^[a-z0-9][a-z0-9_-]{0,31}$` and cannot be `default` (which always means the CLI's live login). The bridge lists what it knows at `GET /v1/sessions`.

Then add one account row per session in Home Assistant, using `oauth_session`:

```yaml
- provider: codex_oauth
  account_id: work
  display_name: Codex (work)
  credential_env: AIUD_OAUTH_BRIDGE_TOKEN
  oauth_bridge_url: https://192.168.42.176:8768
  oauth_bridge_ca_file: /config/bridge-ca.pem
  oauth_session: work
```

Rows without `oauth_session` report the CLI's current live login. Codex refreshes its own tokens inside the captured `auth.json`, so a captured session keeps working until that refresh token is revoked; re-run `capture` after signing in again if a session starts reporting `oauth_session_invalid`. Captured Claude sessions verify sign-in but have no local transcripts, so their token totals are `0`.

Claude Code on macOS may keep its OAuth credentials in the login Keychain instead of `~/.claude/.credentials.json`; in that case `capture` reports `claude: not signed in` and only the Codex half is captured. Verify a captured session once with `CLAUDE_CONFIG_DIR=~/.config/aiud/sessions/NAME/claude claude auth status` before relying on it.

## Metrics and limitations

- `codex_oauth` calls the documented `codex app-server` `account/rateLimits/read` method and publishes OAuth rate-limit windows.
- `claude_code_oauth` verifies Claude Code OAuth status and aggregates token usage from local Claude Code transcript files for the current UTC month. Claude Max remaining quota is not exposed by a supported API, so the bridge does not scrape Claude.ai or claim a remaining-quota value.
- If Codex reports an invalid session, refresh the login on the Mac with `codex login`; the bridge does not accept or store an API key fallback.

## Tests

```sh
python3 -m pytest -q oauth-bridge/test_bridge.py
```

The suite covers token-file permission checks, the plaintext-on-LAN refusal, TLS serving and handshake timeouts, exact bearer-token matching, session-name validation and isolation, credential capture, bounded app-server reads, and malformed transcript records.
