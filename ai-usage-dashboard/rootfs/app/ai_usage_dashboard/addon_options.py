"""Home Assistant add-on options to collector runtime configuration.

Reads ``/data/options.json`` (written by the Supervisor from the add-on
Configuration tab), resolves credential *names* against a mounted secrets
env file plus the process environment, validates everything with actionable
but redacted errors, and writes the YAML runtime config the collector
consumes.

Raw secret values never appear in logs, state topics, discovery payloads,
or the generated config (which holds only env-var *names*).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

from .config import SUPPORTED_PROVIDERS
from .models import AccountConfig, CredentialRef

DEFAULT_MQTT_HOST = "core-mosquitto"  # internal HA Mosquitto broker service
DEFAULT_MQTT_PORT = 1883
DEFAULT_MQTT_PASSWORD_ENV = "MQTT_PASSWORD"
DEFAULT_POLL_INTERVAL = 900
MIN_POLL_INTERVAL = 60
MAX_POLL_INTERVAL = 86400
DEFAULT_DATA_DIR = "/data"
DEFAULT_DISCOVERY_PREFIX = "homeassistant"
DEFAULT_SECRETS_FILE = "/config/secrets.env"
DEFAULT_STATE_FILENAME = "state.json"
DEFAULT_CONFIG_FILENAME = "config.yaml"

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PREFIX_RE = re.compile(r"^[A-Za-z0-9_/-]+$")
_FORBIDDEN_OPTION_KEYS = ("value", "secret", "api_key", "token", "password")
_SECRET_KEY_HINTS = ("password", "passwd", "secret", "token", "api_key", "apikey")


class AddonOptionsError(ValueError):
    """Malformed add-on options or missing credentials (values redacted)."""


def load_options(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        raise AddonOptionsError(
            f"options file {path!r} not found; "
            "this entrypoint runs inside the Home Assistant add-on container "
            "where the Supervisor provides /data/options.json"
        ) from None
    except (OSError, ValueError) as exc:
        raise AddonOptionsError(
            f"options file {path!r} is not valid JSON: {exc}"
        ) from None
    if not isinstance(doc, dict):
        raise AddonOptionsError(
            "options file must contain a JSON object "
            f"(got {type(doc).__name__})"
        )
    return doc


def load_secrets_file(path: str) -> dict[str, str]:
    """Parse a KEY=VALUE env file into a dict. Missing file means no secrets.

    Supports blank lines, `#` comments, an optional `export ` prefix, and
    single/double-quoted values. Malformed lines are ignored (never fatal);
    credential validation later names any variable that stayed unresolved.
    """
    secrets: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise AddonOptionsError(
            f"cannot read secrets file {path!r}: {exc}"
        ) from None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export "):].strip()
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not _ENV_NAME_RE.match(name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        secrets[name] = value
    return secrets


def _merged_env(
    environ: dict | None, secrets: dict | None
) -> dict[str, str]:
    merged = {str(k): str(v) for k, v in (secrets or {}).items()}
    for key, value in (os.environ if environ is None else environ).items():
        merged[str(key)] = str(value)
    return merged


def _require_env_name(value: object, where: str) -> str:
    name = str(value or "").strip()
    if not _ENV_NAME_RE.match(name):
        raise AddonOptionsError(
            f"{where}: must name an env var holding the secret "
            "(e.g. 'OPENAI_API_KEY_PRIMARY'); raw secret values are forbidden"
        )
    return name


def _coerce_int(value: object, where: str, *, default: int) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise AddonOptionsError(
            f"{where}: must be an integer (got {value!r})"
        ) from None


def _first_set(*values: object) -> object:
    """First value that is neither None nor a blank string.

    Unlike `or`-chains, this keeps falsy-but-provided values such as 0 so
    range validation can reject them instead of silently defaulting.
    """
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _account_options(raw: object, where: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AddonOptionsError(f"{where}: 'options' must be a mapping")
    out: dict = {}
    for key, value in raw.items():
        lowered = str(key).lower()
        if lowered in _FORBIDDEN_OPTION_KEYS:
            raise AddonOptionsError(
                f"{where}: option {key!r} looks like a raw secret; "
                "credentials belong in the secrets file, referenced by name"
            )
        if value is None or isinstance(value, (str, int, float, bool)):
            out[str(key)] = value
        else:
            raise AddonOptionsError(
                f"{where}: option {key!r} must be a string, number, or boolean"
            )
    return out


def options_to_runtime(
    options: dict,
    *,
    environ: dict | None = None,
    secrets: dict | None = None,
) -> dict:
    """Validate add-on options and build the collector runtime config.

    Returns ``{"accounts": [AccountConfig], "mqtt": dict,
    "poll_interval": int, "data_dir": str, "state_path": str,
    "discovery_prefix": str, "referenced_env_vars": [...]}``.
    ``referenced_env_vars`` names (never values) the secrets the entrypoint
    must export to the collector process.
    """
    if not isinstance(options, dict):
        raise AddonOptionsError("add-on options must be a JSON object")
    env = _merged_env(environ, secrets)

    raw_accounts = options.get("accounts", [])
    if raw_accounts is None:
        raw_accounts = []
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise AddonOptionsError(
            "'accounts' must be a non-empty list; add at least one account "
            "in the add-on Configuration tab "
            "(provider, account_id, credential_env)"
        )
    accounts: list[AccountConfig] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_accounts):
        where = f"accounts[{i}]"
        if not isinstance(raw, dict):
            raise AddonOptionsError(f"{where} must be a mapping")
        provider = str(raw.get("provider", "")).strip()
        account_id = str(raw.get("account_id", "")).strip()
        display_name = str(
            raw.get("display_name", "") or account_id
        ).strip()
        if provider not in SUPPORTED_PROVIDERS:
            raise AddonOptionsError(
                f"{where}: unknown provider {provider!r}; "
                f"expected one of {list(SUPPORTED_PROVIDERS)}"
            )
        if not account_id:
            raise AddonOptionsError(f"{where}: 'account_id' is required")
        key = f"{provider}/{account_id}"
        if key in seen:
            raise AddonOptionsError(f"{where}: duplicate account {key!r}")
        seen.add(key)
        cred_name = _require_env_name(
            raw.get("credential_env"), f"{where} 'credential_env'"
        )
        if not env.get(cred_name, ""):
            raise AddonOptionsError(
                f"{where}: env var {cred_name!r} is unset or empty; "
                f"add '{cred_name}=<value>' to the secrets file "
                f"({options.get('secrets_file') or DEFAULT_SECRETS_FILE}) "
                "or export it in the add-on environment"
            )
        accounts.append(
            AccountConfig(
                provider=provider,
                account_id=account_id,
                display_name=display_name,
                credential=CredentialRef(env=cred_name),
                options=_account_options(raw.get("options"), where),
            )
        )

    host = (
        str(options.get("mqtt_host", "") or "").strip()
        or env.get("MQTT_HOST", "").strip()
        or DEFAULT_MQTT_HOST
    )
    port = _coerce_int(
        _first_set(options.get("mqtt_port"), env.get("MQTT_PORT")),
        "'mqtt_port'",
        default=DEFAULT_MQTT_PORT,
    )
    if not 1 <= port <= 65535:
        raise AddonOptionsError(
            f"'mqtt_port' must be between 1 and 65535 (got {port})"
        )
    username = (
        str(options.get("mqtt_username", "") or "").strip()
        or env.get("MQTT_USERNAME", "").strip()
    )
    password_env = (
        str(options.get("mqtt_password_env", "") or "").strip()
        or env.get("MQTT_PASSWORD_ENV", "").strip()
        or DEFAULT_MQTT_PASSWORD_ENV
    )
    if username and not env.get(password_env, ""):
        raise AddonOptionsError(
            f"'mqtt_username' is set but env var {password_env!r} is unset "
            "or empty; add it to the secrets file or clear 'mqtt_username' "
            "for anonymous access"
        )
    prefix = (
        str(options.get("discovery_prefix", "") or "").strip()
        or DEFAULT_DISCOVERY_PREFIX
    )
    if not _PREFIX_RE.match(prefix):
        raise AddonOptionsError(
            f"'discovery_prefix' must match {_PREFIX_RE.pattern!r} "
            f"(got {prefix!r})"
        )
    poll_interval = _coerce_int(
        _first_set(options.get("poll_interval"), env.get("AIUD_POLL_INTERVAL")),
        "'poll_interval'",
        default=DEFAULT_POLL_INTERVAL,
    )
    if not MIN_POLL_INTERVAL <= poll_interval <= MAX_POLL_INTERVAL:
        raise AddonOptionsError(
            f"'poll_interval' must be between {MIN_POLL_INTERVAL} and "
            f"{MAX_POLL_INTERVAL} seconds (got {poll_interval})"
        )
    data_dir = str(options.get("data_dir", "") or DEFAULT_DATA_DIR).strip()
    if not data_dir.startswith("/"):
        raise AddonOptionsError(
            f"'data_dir' must be an absolute path (got {data_dir!r}); "
            f"use {DEFAULT_DATA_DIR!r} for persistent add-on storage"
        )
    state_path = os.path.join(data_dir, DEFAULT_STATE_FILENAME)

    mqtt: dict = {
        "host": host,
        "port": port,
        "discovery_prefix": prefix,
    }
    if username:
        mqtt["username"] = username
        mqtt["password_env"] = password_env

    referenced = sorted({a.credential.env for a in accounts if a.credential.env})
    if username:
        referenced = sorted(set(referenced) | {password_env})
    return {
        "accounts": accounts,
        "mqtt": mqtt,
        "poll_interval": poll_interval,
        "data_dir": data_dir,
        "state_path": state_path,
        "discovery_prefix": prefix,
        "referenced_env_vars": referenced,
    }


def _yaml_scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "":
        return "''"
    if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./:+-]*", text) and text not in (
        "true", "True", "TRUE", "false", "False", "FALSE",
        "yes", "Yes", "YES", "no", "No", "NO", "null", "Null", "NULL",
    ):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_runtime_yaml(runtime: dict) -> str:
    """Render the collector YAML config (references only, never secrets)."""
    lines = [
        "# Generated from Home Assistant add-on options; do not edit by hand.",
        "# Credential references only -- raw secrets are never written here.",
        "mqtt:",
        f"  host: {_yaml_scalar(runtime['mqtt']['host'])}",
        f"  port: {_yaml_scalar(runtime['mqtt']['port'])}",
    ]
    if runtime["mqtt"].get("username"):
        lines.append(f"  username: {_yaml_scalar(runtime['mqtt']['username'])}")
        lines.append(
            f"  password_env: {_yaml_scalar(runtime['mqtt']['password_env'])}"
        )
    lines.append(
        f"  discovery_prefix: {_yaml_scalar(runtime['mqtt']['discovery_prefix'])}"
    )
    lines.append("accounts:")
    for acct in runtime["accounts"]:
        lines.append(f"  - provider: {_yaml_scalar(acct.provider)}")
        lines.append(f"    account_id: {_yaml_scalar(acct.account_id)}")
        lines.append(f"    display_name: {_yaml_scalar(acct.display_name)}")
        lines.append("    credential:")
        lines.append(f"      env: {_yaml_scalar(acct.credential.env)}")
        lines.append("    options:")
        for key, value in acct.options.items():
            lines.append(f"      {key}: {_yaml_scalar(value)}")
    return "\n".join(lines) + "\n"


def write_runtime_config(runtime: dict, path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_runtime_yaml(runtime))


def _shell_export(name: str, value: str) -> str:
    return "export {}='{}'".format(name, value.replace("'", "'\\''"))


def write_env_file(
    runtime: dict, merged_env: dict[str, str], path: str
) -> None:
    """Write sourced env exports for the collector process (mode 0600).

    Contains only the referenced secret names plus non-secret runtime
    settings (poll interval, state path, discovery prefix).
    """
    lines = [
        _shell_export("AIUD_POLL_INTERVAL", str(runtime["poll_interval"])),
        _shell_export("AIUD_STATE_FILE", runtime["state_path"]),
        _shell_export("AIUD_DISCOVERY_PREFIX", runtime["discovery_prefix"]),
    ]
    for name in runtime["referenced_env_vars"]:
        lines.append(_shell_export(name, merged_env.get(name, "")))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def redacted_options_for_log(options: dict) -> dict:
    """Copy options with secret-looking values replaced (names are kept)."""

    def redact(obj: object) -> object:
        if isinstance(obj, dict):
            return {
                key: (
                    "***redacted***"
                    if any(h in str(key).lower() for h in _SECRET_KEY_HINTS)
                    and not str(key).lower().endswith("_env")
                    and value not in (None, "", [], {})
                    else redact(value)
                )
                for key, value in obj.items()
            }
        if isinstance(obj, list):
            return [redact(item) for item in obj]
        return obj

    result = redact(options)
    return result if isinstance(result, dict) else {}


def build_runtime_from_options_file(options_path: str) -> tuple[dict, dict]:
    options = load_options(options_path)
    secrets_path = (
        str(options.get("secrets_file", "") or "").strip()
        or DEFAULT_SECRETS_FILE
    )
    secrets = load_secrets_file(secrets_path)
    merged = _merged_env(None, secrets)
    runtime = options_to_runtime(options, environ=dict(os.environ), secrets=secrets)
    return runtime, merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="addon_options",
        description="Validate HA add-on options and write collector runtime config.",
    )
    parser.add_argument("--options", default="/data/options.json")
    parser.add_argument("--write-config", default=None)
    parser.add_argument("--write-env", default=None)
    args = parser.parse_args(argv)
    try:
        runtime, merged = build_runtime_from_options_file(args.options)
    except AddonOptionsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    config_path = args.write_config or os.path.join(
        runtime["data_dir"], DEFAULT_CONFIG_FILENAME
    )
    try:
        write_runtime_config(runtime, config_path)
    except OSError as exc:
        print(f"ERROR: cannot write runtime config: {exc}", file=sys.stderr)
        return 2
    if args.write_env:
        try:
            write_env_file(runtime, merged, args.write_env)
        except OSError as exc:
            print(f"ERROR: cannot write env file: {exc}", file=sys.stderr)
            return 2
    mqtt = runtime["mqtt"]
    auth = f" as {mqtt['username']!r}" if mqtt.get("username") else " (anonymous)"
    print(
        "OK: "
        f"{len(runtime['accounts'])} account(s), "
        f"mqtt={mqtt['host']}:{mqtt['port']}{auth}, "
        f"poll={runtime['poll_interval']}s, "
        f"state={runtime['state_path']}, "
        f"config={config_path}"
    )
    print(f"options (redacted): {json.dumps(redacted_options_for_log(load_options(args.options)))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
