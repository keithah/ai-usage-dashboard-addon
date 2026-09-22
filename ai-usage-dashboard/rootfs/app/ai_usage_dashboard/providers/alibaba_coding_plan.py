"""Alibaba Coding Plan adapter: subscription quota via the official CLI.

Official documented usage command: ``bl usage coding-plan``, which reports
5-hour, weekly, and monthly usage. The official docs do not establish a
stable JSON schema for that command (and do not guarantee
``--output json``), and the official inference endpoints are not quota
endpoints, so this adapter invokes the CLI and parses only the tested
plain-text output shape described in the fixtures. Anything else is
reported as ``unsupported`` (schema drift), never guessed.

Terms warning: Alibaba warns that Coding Plan keys are for interactive
coding tools and may prohibit automated scripts/backends. This adapter
therefore runs only as an explicitly opt-in CLI mode (``opt_in: true``);
without that flag the account reports ``unsupported`` with the warning.
Do not use the undocumented console RPC and do not infer quota from
inference calls.

Options::

    opt_in: true            # required; without it collection is unsupported
    cli_path: bl            # configurable CLI executable (default ``bl``)
    timeout: 30             # per-invocation timeout in seconds (default 30)

The ``bl`` executable is not installed in the default add-on image; point
``cli_path`` at a mounted/bundled executable or extend the image (see the
add-on README/Dockerfile). When the CLI is absent the account reports a
clean ``unavailable``-style ``unsupported`` status, never a fake zero.

Credential handling is secret-reference-only: the resolved secret is
exposed only inside the child process environment (under its configured
env-var name) and is scrubbed from every captured output, reason, state,
and log string.
"""
from __future__ import annotations

import os
import re
import subprocess

from ..models import AccountConfig, AccountSnapshot, Metric, SnapshotStatus, Unit, Window
from .base import CollectContext
from ._helpers import _Auth, fresh, resolve_live_credential, terminal

provider_name = "alibaba_coding_plan"

DEFAULT_CLI_PATH = "bl"
DEFAULT_TIMEOUT = 30.0
MAX_OUTPUT_CHARS = 20000

TERMS_WARNING = (
    "Alibaba warns Coding Plan keys are for interactive coding tools and "
    "automated scripts/backends may be prohibited; enable this polling only "
    "if that use is allowed for your plan"
)

_LEGACY_OPTIONS = ("mode", "region", "api_url")

# (match hints, metric prefix, label, window kind, window label)
_WINDOWS = (
    (("5-hour", "5 hour", "5h"), "quota_5h", "5-hour quota",
     "rolling_5h", "rolling 5h"),
    (("weekly", "week"), "quota_weekly", "Weekly quota",
     "rolling_7d", "rolling 7d"),
    (("monthly", "month", "billing"), "quota_monthly", "Monthly quota",
     "calendar_month", "billing month"),
)

_LINE_RE = re.compile(
    r"(?P<label>[A-Za-z0-9][A-Za-z0-9 _\-]*?)\s*:?\s*"
    r"(?P<used>\d[\d,]*)\s*(?:used\s*)?(?:of|/)\s*(?P<total>\d[\d,]*)",
    re.IGNORECASE,
)
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?")


class Adapter:
    provider_name = "alibaba_coding_plan"

    def collect(self, account: AccountConfig, ctx: CollectContext) -> AccountSnapshot:
        legacy = [k for k in _LEGACY_OPTIONS if k in account.options]
        if legacy:
            return terminal(
                account, SnapshotStatus.ERROR,
                "Alibaba Coding Plan no longer uses console-RPC options "
                f"({', '.join(sorted(legacy))} were removed because the "
                "console RPC is undocumented); switch to the opt-in CLI mode "
                f"with options opt_in: true, cli_path (default {DEFAULT_CLI_PATH!r}), "
                f"timeout. {TERMS_WARNING}.",
            )
        if not _is_truthy(account.options.get("opt_in", False)):
            return terminal(
                account, SnapshotStatus.UNSUPPORTED,
                "Alibaba Coding Plan collection is explicitly opt-in "
                "(set options opt_in: true to run `bl usage coding-plan`); "
                f"{TERMS_WARNING}.",
            )
        cli_path = str(account.options.get("cli_path", DEFAULT_CLI_PATH)).strip()
        if not cli_path:
            return terminal(
                account, SnapshotStatus.ERROR,
                "Alibaba Coding Plan 'cli_path' must be a non-empty executable "
                f"path (default {DEFAULT_CLI_PATH!r})",
            )
        timeout = _coerce_timeout(account.options.get("timeout", DEFAULT_TIMEOUT))
        if timeout is None:
            return terminal(
                account, SnapshotStatus.ERROR,
                f"Alibaba Coding Plan 'timeout' must be a positive number of "
                f"seconds (got {account.options.get('timeout')!r})",
            )
        if ctx.fixture_mode:
            return _from_fixture(account, ctx.account_fixture)
        try:
            secret = resolve_live_credential(account, ctx)
        except _Auth as exc:
            return terminal(account, SnapshotStatus.AUTH_ERROR, str(exc))
        return _run_cli(account, cli_path, timeout, secret,
                        account.credential.env or "")

    def fixture_names(self) -> list[str]:
        return ["cli"]


def _is_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def _coerce_timeout(value: object) -> float | None:
    try:
        timeout = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if timeout <= 0 or timeout > 3600:
        return None
    return timeout


def _scrub(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "***redacted***")
    return text


def _run_cli(account: AccountConfig, cli_path: str, timeout: float,
             secret: str, secret_env_name: str) -> AccountSnapshot:
    child_env = dict(os.environ)
    if secret_env_name:
        child_env[secret_env_name] = secret
    try:
        proc = subprocess.run(
            [cli_path, "usage", "coding-plan"],
            capture_output=True, text=True, timeout=timeout, env=child_env,
        )
    except FileNotFoundError:
        return terminal(
            account, SnapshotStatus.UNSUPPORTED,
            f"Alibaba Coding Plan CLI {cli_path!r} is not installed or not on "
            "PATH, so quota is unavailable; install/bundle the official `bl` "
            "CLI or set options cli_path to its executable. "
            f"{TERMS_WARNING}.",
        )
    except subprocess.TimeoutExpired:
        return terminal(
            account, SnapshotStatus.ERROR,
            f"Alibaba Coding Plan CLI {cli_path!r} timed out after "
            f"{timeout:g}s (`bl usage coding-plan`)",
        )
    except OSError as exc:
        return terminal(
            account, SnapshotStatus.ERROR,
            f"Alibaba Coding Plan CLI {cli_path!r} failed to run: "
            f"{type(exc).__name__}",
        )
    stdout = _scrub(proc.stdout or "", secret)[:MAX_OUTPUT_CHARS]
    stderr = _scrub(proc.stderr or "", secret)[:2000]
    if proc.returncode != 0:
        lowered = stderr.lower()
        if any(hint in lowered for hint in
               ("unauthor", "auth", "login", "token", "forbidden", "denied")):
            return terminal(
                account, SnapshotStatus.AUTH_ERROR,
                f"Alibaba Coding Plan CLI {cli_path!r} reported an "
                f"authentication failure: {stderr.strip() or 'exit ' + str(proc.returncode)}",
            )
        detail = stderr.strip() or f"exit {proc.returncode}"
        return terminal(
            account, SnapshotStatus.ERROR,
            f"Alibaba Coding Plan CLI {cli_path!r} failed: {detail}",
        )
    return _from_text(account, stdout)


def _classify_label(label: str):
    lowered = label.strip().lower()
    for hints, prefix, name, window_kind, window_label in _WINDOWS:
        if any(hint in lowered for hint in hints):
            return prefix, name, window_kind, window_label
    return None


def _parse_line(line: str):
    match = _LINE_RE.search(line)
    if not match:
        return None
    classified = _classify_label(match.group("label"))
    if classified is None:
        return None
    try:
        used = float(match.group("used").replace(",", ""))
        total = float(match.group("total").replace(",", ""))
    except ValueError:
        return None
    end_match = _ISO_RE.search(line)
    return classified, used, total, end_match.group(0) if end_match else None


def _from_text(account: AccountConfig, text: str) -> AccountSnapshot:
    metrics: list[Metric] = []
    seen: set[str] = set()
    missing: list[str] = []
    found = 0
    for line in text.splitlines():
        parsed = _parse_line(line)
        if parsed is None:
            continue
        (prefix, label, window_kind, window_label), used, total, end = parsed
        if prefix in seen:
            continue
        seen.add(prefix)
        found += 1
        window = Window(kind=window_kind, label=window_label, end=end)
        metrics.append(
            Metric(f"{prefix}_used", f"{label} used", used, Unit.COUNT, window)
        )
        metrics.append(
            Metric(f"{prefix}_total", f"{label} total", total, Unit.COUNT, window)
        )
        metrics.append(
            Metric(f"{prefix}_remaining", f"{label} remaining",
                   total - used, Unit.COUNT, window)
        )
        if total > 0:
            metrics.append(
                Metric(f"{prefix}_used_percent", f"{label} used (%)",
                       used / total * 100.0, Unit.PERCENT, window)
            )
    if found == 0:
        return terminal(
            account, SnapshotStatus.UNSUPPORTED,
            "Alibaba Coding Plan CLI output did not match the tested "
            "`bl usage coding-plan` text shape (5-hour/weekly/monthly "
            "'<used> of <total>' lines); refusing to guess quota from "
            "schema drift",
        )
    for _hints, prefix, _label, _kind, _wlabel in _WINDOWS:
        if prefix not in seen:
            missing.append(prefix)
    reason = "subscription quota via opt-in `bl usage coding-plan`"
    if missing:
        reason += "; missing windows: " + ", ".join(missing)
    reason += f". {TERMS_WARNING}."
    return fresh(account, metrics, reason)


def _from_fixture(account: AccountConfig, fixture: dict) -> AccountSnapshot:
    error = (fixture or {}).get("error")
    if error:
        return _snapshot_from_error(account, error)
    cli = (fixture or {}).get("responses", {}).get("cli", {})
    if isinstance(cli, str):
        return _from_text(account, cli)
    if isinstance(cli, dict):
        if cli.get("absent"):
            return terminal(
                account, SnapshotStatus.UNSUPPORTED,
                f"Alibaba Coding Plan CLI {account.options.get('cli_path', DEFAULT_CLI_PATH)!r} "
                "is not installed or not on PATH, so quota is unavailable; "
                "install/bundle the official `bl` CLI or set options cli_path "
                f"to its executable. {TERMS_WARNING}.",
            )
        returncode = cli.get("returncode", 0)
        text = cli.get("text", "")
        if returncode:
            return terminal(
                account, SnapshotStatus.ERROR,
                "Alibaba Coding Plan CLI fixture reported failure: "
                f"{str(cli.get('stderr', '')).strip() or 'exit ' + str(returncode)}",
            )
        return _from_text(account, text if isinstance(text, str) else "")
    return terminal(
        account, SnapshotStatus.UNSUPPORTED,
        "Alibaba Coding Plan fixture has no tested `bl usage coding-plan` "
        "text under responses.cli",
    )


def _snapshot_from_error(account: AccountConfig, error: dict) -> AccountSnapshot:
    kind = error.get("kind", "error")
    message = error.get("message", "fixture error")
    if kind == "auth":
        return terminal(account, SnapshotStatus.AUTH_ERROR, message)
    if kind == "unsupported":
        return terminal(account, SnapshotStatus.UNSUPPORTED, message)
    if kind in ("transient", "rate_limit"):
        from .openai import _Transient

        raise _Transient(message)
    return terminal(account, SnapshotStatus.ERROR, message)
