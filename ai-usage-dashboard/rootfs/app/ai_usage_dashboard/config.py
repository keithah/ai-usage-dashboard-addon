"""Configuration loading and validation.

Supports YAML config files. Uses PyYAML when installed, otherwise falls back
to a small built-in parser covering the documented config subset (nested
mappings, lists of mappings, scalars, comments). Live credentials are only
ever referenced via env vars or macOS keychain entries, never inline.
"""
from __future__ import annotations

from .models import AccountConfig, CredentialRef

SUPPORTED_PROVIDERS = (
    "openai",
    "anthropic",
    "kimi",
    "deepseek",
    "opencode_go",
    "muse_code",
    "alibaba_coding_plan",
    "grok",
    "openrouter",
    "gemini",
    "coderabbit",
)


class ConfigError(ValueError):
    pass


def _parse_scalar(text: str):
    t = text.strip()
    if t == "" or t in ("null", "~", "None"):
        return None
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        return t[1:-1]
    low = t.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def _strip_comment(raw: str) -> str:
    # Cut at ' #' but keep URLs (which contain '://', never ' #').
    idx = raw.find(" #")
    if idx != -1:
        return raw[:idx]
    if raw.lstrip().startswith("#"):
        return ""
    return raw


def _minimal_yaml_load(text: str) -> dict:
    """Parse the documented config subset: mappings, lists, scalars, comments."""
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        code = _strip_comment(raw)
        if not code.strip():
            continue
        indent = len(code) - len(code.lstrip(" "))
        lines.append((indent, code.strip()))
    if not lines:
        return {}

    def parse_mapping(pos: int, indent: int) -> tuple[dict, int]:
        mapping: dict = {}
        while pos < len(lines) and lines[pos][0] == indent:
            content = lines[pos][1]
            if content == "-" or content.startswith("- "):
                raise ConfigError(f"unexpected list item near: {content!r}")
            if ":" not in content:
                raise ConfigError(f"expected 'key: value' near: {content!r}")
            key, _, rest = content.partition(":")
            key = key.strip()
            pos += 1
            if rest.strip():
                mapping[key] = _parse_scalar(rest)
            elif pos < len(lines) and lines[pos][0] > indent:
                child_indent = lines[pos][0]
                child = lines[pos][1]
                if child == "-" or child.startswith("- "):
                    mapping[key], pos = parse_list(pos, child_indent)
                else:
                    mapping[key], pos = parse_mapping(pos, child_indent)
            else:
                mapping[key] = None
        return mapping, pos

    def parse_list(pos: int, indent: int) -> tuple[list, int]:
        items: list = []
        while pos < len(lines) and lines[pos][0] == indent and (
            lines[pos][1] == "-" or lines[pos][1].startswith("- ")
        ):
            content = lines[pos][1][1:].strip()
            pos += 1
            if not content:
                if pos < len(lines) and lines[pos][0] > indent:
                    child_indent = lines[pos][0]
                    child = lines[pos][1]
                    if child == "-" or child.startswith("- "):
                        item, pos = parse_list(pos, child_indent)
                    else:
                        item, pos = parse_mapping(pos, child_indent)
                    items.append(item)
                else:
                    items.append(None)
            elif ":" in content and not content.startswith(("'", '"')):
                # "- key: value" opens a mapping item at virtual indent.
                key, _, rest = content.partition(":")
                sub: dict = {}
                if rest.strip():
                    sub[key.strip()] = _parse_scalar(rest)
                elif pos < len(lines) and lines[pos][0] > indent:
                    child_indent = lines[pos][0]
                    child = lines[pos][1]
                    if child == "-" or child.startswith("- "):
                        sub[key.strip()], pos = parse_list(pos, child_indent)
                    else:
                        sub[key.strip()], pos = parse_mapping(pos, child_indent)
                else:
                    sub[key.strip()] = None
                while pos < len(lines) and lines[pos][0] > indent and (
                    ":" in lines[pos][1]
                    and not lines[pos][1].startswith(("- ", "-"))
                ):
                    key2, _, rest2 = lines[pos][1].partition(":")
                    key2 = key2.strip()
                    pos += 1
                    if rest2.strip():
                        sub[key2] = _parse_scalar(rest2)
                    elif pos < len(lines) and lines[pos][0] > lines[pos - 1][0]:
                        child_indent = lines[pos][0]
                        child = lines[pos][1]
                        if child == "-" or child.startswith("- "):
                            sub[key2], pos = parse_list(pos, child_indent)
                        else:
                            sub[key2], pos = parse_mapping(pos, child_indent)
                    else:
                        sub[key2] = None
                items.append(sub)
            else:
                items.append(_parse_scalar(content))
        return items, pos

    first = lines[0][1]
    if first == "-" or first.startswith("- "):
        raise ConfigError("top-level config must be a mapping")
    doc, pos = parse_mapping(0, lines[0][0])
    if pos != len(lines):
        raise ConfigError("trailing content could not be parsed")
    return doc


def load_raw(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        import yaml  # type: ignore

        doc = yaml.safe_load(text)
        return doc if isinstance(doc, dict) else {}
    except ImportError:
        return _minimal_yaml_load(text)


def _credential_from(raw: object, where: str) -> CredentialRef:
    if raw is None:
        return CredentialRef()
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: credential must be a mapping")
    for forbidden in ("value", "secret", "api_key", "token", "password"):
        if forbidden in raw:
            raise ConfigError(
                f"{where}: raw secrets are forbidden in config; "
                "use {env: VAR_NAME} or keychain references"
            )
    return CredentialRef(
        env=raw.get("env"),
        keychain_service=raw.get("keychain_service"),
        keychain_account=raw.get("keychain_account"),
    )


def load_config(path: str) -> dict:
    """Load config file into {'accounts': [AccountConfig], 'mqtt': dict}."""
    doc = load_raw(path)
    return build_config(doc)


def build_config(doc: dict) -> dict:
    raw_accounts = doc.get("accounts", [])
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise ConfigError("'accounts' must be a non-empty list")
    accounts: list[AccountConfig] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_accounts):
        where = f"accounts[{i}]"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where} must be a mapping")
        provider = str(raw.get("provider", "")).strip()
        account_id = str(raw.get("account_id", "")).strip()
        display_name = str(raw.get("display_name", account_id)).strip()
        if provider not in SUPPORTED_PROVIDERS:
            raise ConfigError(
                f"{where}: unknown provider {provider!r}; "
                f"expected one of {list(SUPPORTED_PROVIDERS)}"
            )
        if not account_id:
            raise ConfigError(f"{where}: 'account_id' is required")
        key = f"{provider}/{account_id}"
        if key in seen:
            raise ConfigError(f"{where}: duplicate account {key!r}")
        seen.add(key)
        options = raw.get("options", {}) or {}
        if not isinstance(options, dict):
            raise ConfigError(f"{where}: 'options' must be a mapping")
        accounts.append(
            AccountConfig(
                provider=provider,
                account_id=account_id,
                display_name=display_name,
                credential=_credential_from(raw.get("credential"), where),
                options=dict(options),
            )
        )
    mqtt = doc.get("mqtt", {}) or {}
    if not isinstance(mqtt, dict):
        raise ConfigError("'mqtt' must be a mapping")
    return {"accounts": accounts, "mqtt": dict(mqtt)}


def validate_file(path: str) -> list[str]:
    """Return a list of validation error strings (empty structural errors)."""
    try:
        cfg = load_config(path)
    except (ConfigError, OSError) as exc:
        return [str(exc)]
    problems: list[str] = []
    for acct in cfg["accounts"]:
        if acct.credential.is_empty():
            problems.append(
                f"{acct.key}: no credential reference; add a "
                "{env: VAR} or keychain reference "
                "(fixture mode does not need one)"
            )
    return problems
