#!/usr/bin/env python3
"""Metrics-only OAuth bridge for the AI Usage Dashboard.

This process stays on the machine that owns the provider OAuth sessions. It
never accepts provider tokens, never forwards arbitrary commands, and returns
only normalized metrics to the Home Assistant add-on.
"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import re
import select
import shutil
import ssl
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit


class BridgeError(RuntimeError):
    """Safe, non-secret bridge failure."""


class _AppServerClient:
    max_buffer = 4 * 1024 * 1024

    def __init__(self, command: list[str], timeout: float = 20.0, env: dict[str, str] | None = None):
        self.command = command
        self.timeout = timeout
        self.env = env

    def rate_limits(self) -> dict[str, Any]:
        try:
            proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self.env,
            )
        except OSError as exc:
            raise BridgeError(f"cannot start OAuth CLI: {type(exc).__name__}") from None
        try:
            self._send(proc, {"method": "initialize", "id": 1, "params": {
                "clientInfo": {
                    "name": "ai_usage_dashboard_oauth_bridge",
                    "title": "AI Usage Dashboard OAuth Bridge",
                    "version": "0.1.0",
                }
            }})
            self._response(proc, 1)
            self._send(proc, {"method": "initialized", "params": {}})
            self._send(proc, {"method": "account/rateLimits/read", "id": 2, "params": {}})
            response = self._response(proc, 2)
            if "error" in response:
                # Do not forward the CLI's response body; it can contain
                # account or upstream diagnostics.
                return {"authenticated": False, "error": "oauth_session_invalid"}
            return normalize_codex_rate_limits(response.get("result", {}))
        finally:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)

    def _send(self, proc: subprocess.Popen, message: dict[str, Any]) -> None:
        if not proc.stdin:
            raise BridgeError("OAuth CLI input is unavailable")
        proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        proc.stdin.flush()

    def _response(self, proc: subprocess.Popen, wanted_id: int) -> dict[str, Any]:
        if not proc.stdout:
            raise BridgeError("OAuth CLI output is unavailable")
        # Bounded, non-blocking reads: select() only proves *some* bytes are
        # ready, so a plain readline() could block forever on a partial line.
        fd = proc.stdout.fileno()
        buffer = b""
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeError("OAuth CLI timed out")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise BridgeError("OAuth CLI timed out")
            chunk = os.read(fd, 65536)
            if not chunk:
                raise BridgeError("OAuth CLI exited before returning metrics")
            buffer += chunk
            if len(buffer) > self.max_buffer:
                raise BridgeError("OAuth CLI output exceeded buffer limit")
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(message, dict) and message.get("id") == wanted_id:
                    return message


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_codex_rate_limits(payload: Any) -> dict[str, Any]:
    """Reduce Codex app-server ``account/rateLimits/read`` output to a secret-free document.

    Live shape (verified against codex app-server): ``result.rateLimits`` is a
    dict with window entries ``primary``/``secondary`` of
    ``{usedPercent, windowDurationMins, resetsAt}`` plus non-window metadata
    (``planType``, ``credits``, ``limitId`` ...). Older/alternate spellings
    (``limitWindowSeconds``, ``resetAt``, snake_case, list form) are still
    accepted. Only numeric metrics are copied; ids, titles and tokens are not.
    """
    if not isinstance(payload, dict):
        payload = {}
    raw = payload.get("rateLimits") or payload.get("rate_limits") or {}
    limits: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        entries = raw.items()
    elif isinstance(raw, list):
        entries = ((str(item.get("name", "window")), item) for item in raw if isinstance(item, dict))
    else:
        entries = []
    for name, value in entries:
        if not isinstance(value, dict):
            continue
        used = _number(value.get("usedPercent", value.get("used_percent")))
        window = _number(value.get("limitWindowSeconds", value.get("window_seconds")))
        if window is None:
            mins = _number(value.get("windowDurationMins", value.get("window_minutes")))
            window = mins * 60 if mins is not None else None
        reset = value.get("resetsAt", value.get("resetAt", value.get("reset_at")))
        if used is None and window is None and reset is None:
            continue
        item: dict[str, Any] = {"name": str(name)}
        if used is not None:
            item["used_percent"] = used
            item["remaining_percent"] = max(0.0, min(100.0, 100.0 - used))
        if window is not None:
            item["window_seconds"] = int(window) if window.is_integer() else window
        if isinstance(reset, (int, float)) and not isinstance(reset, bool):
            item["reset_at"] = reset
        limits.append(item)
    plan = None
    if isinstance(raw, dict):
        plan = raw.get("planType", raw.get("plan_type"))
    if plan is None:
        plan = payload.get("planType", payload.get("plan_type"))
    return {
        "authenticated": True,
        "auth_mode": "oauth",
        "plan_type": plan if isinstance(plan, str) else None,
        "rate_limits": limits,
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def aggregate_claude_usage(projects_dir: Path, *, now: datetime | None = None) -> dict[str, int]:
    """Aggregate Claude Code transcript token counts for the current UTC month."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    matched_files: set[Path] = set()
    # Claude Code can write the same API response to more than one transcript
    # line/file (resumed sessions, sidechains); count each response once.
    seen: set[tuple[str, str]] = set()
    if not projects_dir.is_dir():
        totals["total_tokens"] = 0
        totals["session_files"] = 0
        return totals
    start_ts = start.timestamp()
    for path in projects_dir.rglob("*.jsonl"):
        try:
            # Files untouched since before the month started cannot hold
            # in-window records; skip them without parsing.
            if path.stat().st_mtime < start_ts:
                continue
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    timestamp = _parse_timestamp(record.get("timestamp"))
                    message = record.get("message")
                    usage = message.get("usage") if isinstance(message, dict) else None
                    if timestamp is None or timestamp < start or not isinstance(usage, dict):
                        continue
                    msg_id = message.get("id")
                    req_id = record.get("requestId")
                    if isinstance(msg_id, str) and isinstance(req_id, str):
                        key = (msg_id, req_id)
                        if key in seen:
                            continue
                        seen.add(key)
                    matched_files.add(path)
                    for key in totals:
                        if key == "total_tokens":
                            continue
                        value = _number(usage.get(key))
                        if value is not None and value >= 0:
                            totals[key] += int(value)
        except (OSError, UnicodeError):
            continue
    totals["total_tokens"] = sum(totals[key] for key in (
        "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"
    ))
    totals["session_files"] = len(matched_files)
    return totals


def claude_auth_status(command: list[str], timeout: float, env: dict[str, str] | None = None) -> bool:
    try:
        completed = subprocess.run(
            command + ["auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("loggedIn", payload.get("logged_in", False)))


def claude_usage_document(
    claude_bin: str, projects_dir: Path, timeout: float, env: dict[str, str] | None = None,
    captured: bool = False,
) -> dict[str, Any]:
    authenticated = claude_auth_status([claude_bin], timeout, env=env)
    has_transcripts = projects_dir.is_dir()
    totals = aggregate_claude_usage(projects_dir)  # returns zero totals for a non-directory
    quota = "Claude Max remaining quota is not exposed by a supported API."
    if has_transcripts:
        note = f"Consumed local Claude Code transcript usage; {quota}"
    elif captured:
        note = f"Sign-in verified; captured sessions hold only credentials, not transcripts, so token totals are 0. {quota}"
    else:
        note = f"Sign-in verified, but no Claude Code transcript directory was found at {projects_dir}, so token totals are 0. {quota}"
    return {
        "authenticated": authenticated,
        "auth_mode": "oauth",
        "window": "calendar_month",
        "metrics": totals,
        "quota_available": False,
        "note": note,
    }


_SESSION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
DEFAULT_SESSION = "default"


class SessionError(KeyError):
    """Unknown or malformed session name (safe to report as 404)."""


class _Sessions:
    """Map a session name to isolated CLI homes so several OAuth accounts coexist.

    Layout under ``sessions_dir``::

        <name>/codex/    -> CODEX_HOME for that account (``codex login`` there)
        <name>/claude/   -> CLAUDE_CONFIG_DIR for that account (``claude login`` there)

    ``default`` uses the CLIs' normal homes. Names are validated against a
    strict pattern *before* any path is built, so a request can never escape
    ``sessions_dir``; the directory must also already exist (no auto-create).
    """

    def __init__(self, sessions_dir: Path, base_env: dict[str, str], default_projects_dir: Path):
        self.sessions_dir = sessions_dir
        self.base_env = dict(base_env)
        self.default_projects_dir = default_projects_dir

    def _dir(self, name: str, tool: str) -> Path | None:
        if name == DEFAULT_SESSION:
            return None
        if not _SESSION_NAME_RE.fullmatch(name):
            raise SessionError(name)
        home = self.sessions_dir / name / tool
        # Never follow symlinks: a session must be a real directory *inside*
        # sessions_dir so the "cannot escape" guarantee holds literally.
        if (self.sessions_dir / name).is_symlink() or home.is_symlink() or not home.is_dir():
            raise SessionError(name)
        return home

    def codex_env(self, name: str) -> dict[str, str]:
        home = self._dir(name, "codex")
        env = dict(self.base_env)
        if home is not None:
            env["CODEX_HOME"] = str(home)
        return env

    def claude_env(self, name: str) -> tuple[dict[str, str], Path]:
        home = self._dir(name, "claude")
        env = dict(self.base_env)
        if home is None:
            return env, self.default_projects_dir
        env["CLAUDE_CONFIG_DIR"] = str(home)
        return env, home / "projects"

    def names(self) -> list[str]:
        names = [DEFAULT_SESSION]
        if self.sessions_dir.is_dir():
            for child in sorted(self.sessions_dir.iterdir()):
                if (
                    not child.is_symlink()
                    and child.is_dir()
                    and _SESSION_NAME_RE.fullmatch(child.name)
                    and child.name != DEFAULT_SESSION
                ):
                    names.append(child.name)
        return names


class _Service:
    def __init__(
        self,
        token: str,
        *,
        codex_bin: str,
        claude_bin: str,
        projects_dir: Path,
        timeout: float,
        sessions_dir: Path | None = None,
    ):
        self.token = token
        self.codex_bin = codex_bin
        self.claude_bin = claude_bin
        self.timeout = timeout
        self.sessions = _Sessions(
            sessions_dir or Path.home() / ".config" / "aiud" / "sessions",
            base_env=dict(os.environ),
            default_projects_dir=projects_dir,
        )
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, tool: str, session: str) -> threading.Lock:
        # Serialize only same-(tool, session) work: two concurrent `codex
        # app-server` spawns against one CODEX_HOME could race the token refresh
        # in auth.json. Different sessions/tools proceed in parallel.
        with self._locks_guard:
            return self._locks.setdefault((tool, session), threading.Lock())

    def get(self, path: str, session: str = DEFAULT_SESSION) -> dict[str, Any]:
        if path == "/v1/codex/rate-limits":
            env = self.sessions.codex_env(session)
            with self._lock_for("codex", session):
                return _AppServerClient([self.codex_bin, "app-server"], self.timeout, env=env).rate_limits()
        if path == "/v1/claude/usage":
            env, projects_dir = self.sessions.claude_env(session)
            with self._lock_for("claude", session):
                return claude_usage_document(
                    self.claude_bin, projects_dir, self.timeout, env=env, captured=session != DEFAULT_SESSION
                )
        raise KeyError(path)


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def make_handler(service: _Service):
    class Handler(BaseHTTPRequestHandler):
        # Bound how long a client may hold a handler thread (slowloris).
        timeout = 10.0

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            path = parts.path
            if path == "/health":
                self._write(200, {
                    "status": "ok",
                    "capabilities": ["codex_rate_limits", "claude_local_usage", "sessions"],
                })
                return
            expected = ("Bearer " + service.token).encode("utf-8")
            supplied = self.headers.get("Authorization", "").encode("utf-8")
            if not hmac.compare_digest(supplied, expected):
                self._write(401, {"error": "unauthorized"})
                return
            if path == "/v1/sessions":
                self._write(200, {"sessions": service.sessions.names()})
                return
            query = parse_qs(parts.query, keep_blank_values=True)
            session_values = query.get("session", [DEFAULT_SESSION])
            if len(session_values) != 1:
                self._write(400, {"error": "bad_session"})
                return
            try:
                self._write(200, service.get(path, session=session_values[0]))
            except SessionError:
                self._write(404, {"error": "unknown_session"})
            except KeyError:
                self._write(404, {"error": "not_found"})
            except BridgeError:
                self._write(502, {"error": "oauth_upstream_unavailable"})
            except Exception:
                self._write(500, {"error": "bridge_failure"})

        def _write(self, status: int, payload: dict[str, Any]) -> None:
            body = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


class TokenFileError(ValueError):
    """The bearer token file is unusable or exposed to other users."""


def _read_token(path: str) -> str:
    token_path = Path(path)
    try:
        info = token_path.stat()
    except OSError as exc:
        raise TokenFileError(f"cannot read token file: {type(exc).__name__}") from None
    if not stat.S_ISREG(info.st_mode):
        raise TokenFileError("token file must be a regular file")
    if info.st_uid != os.getuid():
        raise TokenFileError("token file must be owned by the user running the bridge")
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise TokenFileError("token file must not be readable by group or others (chmod 600)")
    token = token_path.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise TokenFileError("token file must contain at least 32 characters")
    return token


def _is_loopback(host: str) -> bool:
    # '' means INADDR_ANY to socket.bind, so it must NOT count as loopback.
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _private_key_is_protected(path: str) -> bool:
    try:
        info = Path(path).stat()
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and not info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    )


class _TlsServer(ThreadingHTTPServer):
    """TLS server that handshakes per-connection in the handler thread.

    Wrapping the *listening* socket makes accept() perform the handshake on
    the single serve_forever thread, so one idle TCP client stalls everyone.
    Instead we wrap each accepted socket with do_handshake_on_connect=False
    and complete the handshake under a timeout inside the handler thread.
    """

    handshake_timeout = 10.0

    def __init__(self, address, handler, context: ssl.SSLContext):
        super().__init__(address, handler)
        self._tls_context = context

    def get_request(self):
        sock, addr = super().get_request()
        sock.settimeout(self.handshake_timeout)
        try:
            tls_sock = self._tls_context.wrap_socket(
                sock, server_side=True, do_handshake_on_connect=False
            )
        except (OSError, ssl.SSLError):
            sock.close()
            raise
        return tls_sock, addr

    def finish_request(self, request, client_address):
        try:
            request.do_handshake()
        except (OSError, ssl.SSLError):
            # Bad or absent ClientHello; drop quietly without touching the handler.
            return
        request.settimeout(None)
        super().finish_request(request, client_address)

    def handle_error(self, request, client_address):
        # Never print stack traces: they could include request details.
        return


def build_server(args: argparse.Namespace, service: _Service) -> ThreadingHTTPServer:
    """Bind the HTTP(S) server, refusing to expose the bearer token in plaintext.

    Non-loopback binds require TLS so the bridge token cannot be sniffed or
    replayed from the LAN. Loopback binds may run plaintext because the token
    never leaves the host there.
    """
    tls = bool(args.tls_cert or args.tls_key)
    if tls and not (args.tls_cert and args.tls_key):
        raise SystemExit("--tls-cert and --tls-key must be supplied together")
    if not tls and not _is_loopback(args.host):
        raise SystemExit(
            f"refusing to bind {args.host!r} without TLS: pass --tls-cert/--tls-key "
            "(see README) or bind to 127.0.0.1"
        )
    if tls and not _private_key_is_protected(args.tls_key):
        raise SystemExit("TLS private key must be a regular file owned by you with mode 600")
    handler = make_handler(service)
    if not tls:
        return ThreadingHTTPServer((args.host, args.port), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=args.tls_cert, keyfile=args.tls_key)
    return _TlsServer((args.host, args.port), handler, context)


def _resolve_cli(name_or_path: str, label: str) -> str:
    """Resolve a CLI to an absolute path at startup so a bad PATH fails loudly.

    launchd agents run with a minimal PATH; silently failing to find the CLI
    would surface as a permanent 'not authenticated' answer instead.
    """
    resolved = shutil.which(name_or_path)
    if not resolved:
        raise SystemExit(f"{label} CLI not found: {name_or_path!r} (set --{label.lower()}-bin or PATH)")
    return resolved


# Exactly the files each CLI needs to consider itself signed in. Nothing else
# (transcripts, caches, memories, config) is copied.
_CAPTURE_FILES = {
    "codex": ("auth.json",),
    "claude": (".credentials.json",),
}


def _copy_private(src: Path, dst: Path) -> None:
    if dst.parent.is_symlink():
        raise SystemExit(f"refusing to write through symlink {dst.parent}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    # O_EXCL|O_NOFOLLOW: never write through a pre-planted symlink; a stale
    # .tmp from an interrupted run is removed (unlink, not followed) first.
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    if dst.is_symlink():
        dst.unlink()
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(src.read_bytes())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, dst)


def capture_session(
    name: str,
    sessions_dir: Path,
    *,
    tools: tuple[str, ...] = ("codex", "claude"),
    codex_home: Path | None = None,
    claude_config_dir: Path | None = None,
) -> dict[str, str]:
    """Snapshot the currently signed-in CLI credentials into ``sessions_dir/<name>``.

    Workflow for several accounts on one Mac: sign in to account A, run
    ``bridge.py capture a``, sign out, sign in to account B, run
    ``bridge.py capture b``. Each session dir then answers as that account.
    Returns {tool: "captured" | "not signed in"} for the tools requested.
    """
    if name == DEFAULT_SESSION or not _SESSION_NAME_RE.fullmatch(name):
        raise SystemExit(
            f"invalid session name {name!r}: 1-32 lowercase letters/digits/'-'/'_' (not 'default')"
        )
    sources = {
        "codex": codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"),
        "claude": claude_config_dir or Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"),
    }
    sessions_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(sessions_dir, 0o700)
    results: dict[str, str] = {}
    for tool in tools:
        if tool not in _CAPTURE_FILES:
            raise SystemExit(f"unknown tool {tool!r}")
        src_dir = sources[tool]
        files = [src_dir / f for f in _CAPTURE_FILES[tool]]
        dest_dir = sessions_dir / name / tool
        if not all(f.is_file() for f in files):
            # Don't leave a stale credential from an earlier capture behind
            # for a tool that is no longer signed in.
            for f in _CAPTURE_FILES[tool]:
                stale = dest_dir / f
                if stale.is_symlink() or stale.exists():
                    stale.unlink()
            results[tool] = "not signed in"
            continue
        if (sessions_dir / name).is_symlink() or dest_dir.is_symlink():
            raise SystemExit(f"refusing to capture into symlinked session {name!r}")
        dest_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(sessions_dir / name, 0o700)
        os.chmod(dest_dir, 0o700)
        for f in files:
            _copy_private(f, dest_dir / f.name)
        results[tool] = "captured"
    return results


def _capture_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="bridge.py capture", description=capture_session.__doc__)
    parser.add_argument("name", help="session name to store the current CLI sign-in under")
    parser.add_argument("--only", choices=sorted(_CAPTURE_FILES), action="append",
                        help="capture just this tool (repeatable); default: all")
    parser.add_argument(
        "--sessions-dir",
        default=os.environ.get("AIUD_SESSIONS_DIR", str(Path.home() / ".config" / "aiud" / "sessions")),
    )
    args = parser.parse_args(argv)
    tools = tuple(args.only) if args.only else tuple(_CAPTURE_FILES)
    results = capture_session(args.name, Path(args.sessions_dir).expanduser(), tools=tools)
    for tool, status in results.items():
        print(f"{tool}: {status}")
    if all(v == "not signed in" for v in results.values()):
        print("nothing captured: sign in with `codex login` / `claude login` first", flush=True)
        return 1
    print(f"session {args.name!r} ready; use oauth_session: {args.name} in the add-on", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "capture":
        return _capture_main(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("AIUD_BRIDGE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AIUD_BRIDGE_PORT", "8765")))
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--tls-cert", default=os.environ.get("AIUD_BRIDGE_TLS_CERT"))
    parser.add_argument("--tls-key", default=os.environ.get("AIUD_BRIDGE_TLS_KEY"))
    parser.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN", "codex"))
    parser.add_argument("--claude-bin", default=os.environ.get("CLAUDE_BIN", "claude"))
    parser.add_argument("--claude-projects-dir", default=os.environ.get("CLAUDE_PROJECTS_DIR", str(Path.home() / ".claude" / "projects")))
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="per-CLI-call timeout in seconds; keep below the add-on poll interval")
    parser.add_argument(
        "--sessions-dir",
        default=os.environ.get("AIUD_SESSIONS_DIR", str(Path.home() / ".config" / "aiud" / "sessions")),
        help="Directory holding <session>/codex and <session>/claude CLI homes for extra accounts",
    )
    args = parser.parse_args(argv)
    try:
        token = _read_token(args.token_file)
    except TokenFileError as exc:
        raise SystemExit(str(exc)) from None
    service = _Service(
        token,
        codex_bin=_resolve_cli(args.codex_bin, "Codex"),
        claude_bin=_resolve_cli(args.claude_bin, "Claude"),
        projects_dir=Path(args.claude_projects_dir).expanduser(),
        timeout=args.timeout,
        sessions_dir=Path(args.sessions_dir).expanduser(),
    )
    server = build_server(args, service)
    scheme = "https" if args.tls_cert else "http"
    print(f"AIUD OAuth bridge listening on {scheme}://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
