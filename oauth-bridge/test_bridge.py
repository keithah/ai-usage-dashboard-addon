import argparse
import http.client
import json
import os
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import bridge
from bridge import (
    TokenFileError,
    _Service,
    _read_token,
    aggregate_claude_usage,
    build_server,
    make_handler,
    normalize_codex_rate_limits,
)


def test_normalize_codex_rate_limits_returns_only_normalized_oauth_metrics():
    result = normalize_codex_rate_limits(
        {
            "planType": "plus",
            "rateLimits": {
                "primary": {
                    "usedPercent": 37.5,
                    "limitWindowSeconds": 18000,
                    "resetAt": 1790000000,
                },
                "secondary": {
                    "usedPercent": 12,
                    "limitWindowSeconds": 604800,
                    "resetAt": 1790500000,
                },
            },
            "accessToken": "must-not-leak",
        }
    )

    assert result["auth_mode"] == "oauth"
    assert result["plan_type"] == "plus"
    assert result["rate_limits"] == [
        {
            "name": "primary",
            "used_percent": 37.5,
            "remaining_percent": 62.5,
            "window_seconds": 18000,
            "reset_at": 1790000000,
        },
        {
            "name": "secondary",
            "used_percent": 12.0,
            "remaining_percent": 88.0,
            "window_seconds": 604800,
            "reset_at": 1790500000,
        },
    ]
    assert "accessToken" not in json.dumps(result)


def _usage_record(ts: str, input_tokens: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": ts,
            "message": {"usage": {"input_tokens": input_tokens, "output_tokens": 1}},
        }
    )


def test_aggregate_claude_usage_sums_current_month_without_returning_transcript_data(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    path = projects / "session.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-09-20T12:00:00Z",
                        "message": {
                            "usage": {
                                "input_tokens": 100,
                                "output_tokens": 25,
                                "cache_creation_input_tokens": 10,
                                "cache_read_input_tokens": 5,
                            }
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-08-20T12:00:00Z",
                        "message": {
                            "usage": {"input_tokens": 999, "output_tokens": 999}
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = aggregate_claude_usage(
        projects,
        now=datetime(2026, 9, 28, tzinfo=timezone.utc),
    )

    assert result == {
        "input_tokens": 100,
        "output_tokens": 25,
        "cache_creation_input_tokens": 10,
        "cache_read_input_tokens": 5,
        "total_tokens": 140,
        "session_files": 1,
    }
    assert "message" not in json.dumps(result)


def test_aggregate_claude_usage_skips_malformed_records_and_keeps_counting(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "session.jsonl").write_text(
        "\n".join(
            [
                _usage_record("2026-09-01T00:00:00Z", 10),
                json.dumps({"timestamp": "2026-09-02T00:00:00Z", "message": "not a mapping"}),
                json.dumps({"timestamp": "2026-09-02T00:00:00Z", "message": ["list"]}),
                json.dumps({"timestamp": "2026-09-02T00:00:00Z", "message": None}),
                json.dumps(["top-level list, not a record"]),
                json.dumps("bare string"),
                "{not json",
                _usage_record("2026-09-03T00:00:00Z", 20),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = aggregate_claude_usage(projects, now=datetime(2026, 9, 28, tzinfo=timezone.utc))

    # Both valid records were aggregated even though malformed lines sat between them.
    assert result["input_tokens"] == 30
    assert result["output_tokens"] == 2
    assert result["session_files"] == 1


def _write_token(path: Path, mode: int = 0o600, value: str = "x" * 64) -> Path:
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_read_token_accepts_owner_only_regular_file(tmp_path):
    path = _write_token(tmp_path / "token")
    assert _read_token(str(path)) == "x" * 64


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660, 0o666])
def test_read_token_rejects_group_or_world_accessible_file(tmp_path, mode):
    path = _write_token(tmp_path / "token", mode=mode)
    with pytest.raises(TokenFileError, match="chmod 600"):
        _read_token(str(path))


def test_read_token_rejects_short_token(tmp_path):
    path = _write_token(tmp_path / "token", value="short")
    with pytest.raises(TokenFileError, match="32 characters"):
        _read_token(str(path))


def test_read_token_rejects_missing_or_non_regular_file(tmp_path):
    with pytest.raises(TokenFileError):
        _read_token(str(tmp_path / "missing"))
    directory = tmp_path / "dir"
    directory.mkdir()
    with pytest.raises(TokenFileError, match="regular file"):
        _read_token(str(directory))


def _args(**overrides) -> argparse.Namespace:
    base = {"host": "127.0.0.1", "port": 0, "tls_cert": None, "tls_key": None}
    base.update(overrides)
    return argparse.Namespace(**base)


def _service() -> _Service:
    return _Service("t" * 64, codex_bin="codex", claude_bin="claude", projects_dir=Path("/nonexistent"), timeout=1.0)


def test_build_server_refuses_plaintext_on_non_loopback_bind():
    # '' is INADDR_ANY (binds every interface) and must be refused like 0.0.0.0.
    for host in ("0.0.0.0", "192.168.42.176", "::", ""):
        with pytest.raises(SystemExit, match="without TLS"):
            build_server(_args(host=host), _service())


def test_build_server_rejects_tls_key_not_owned_by_user(tmp_path, monkeypatch):
    cert, key = _self_signed(tmp_path)
    real_uid = os.getuid()
    monkeypatch.setattr(bridge.os, "getuid", lambda: real_uid + 1)
    with pytest.raises(SystemExit, match="owned by you"):
        build_server(_args(host="0.0.0.0", tls_cert=str(cert), tls_key=str(key)), _service())


def test_build_server_requires_both_tls_flags(tmp_path):
    with pytest.raises(SystemExit, match="together"):
        build_server(_args(host="0.0.0.0", tls_cert=str(tmp_path / "c.pem")), _service())


def test_build_server_allows_plaintext_on_loopback():
    server = build_server(_args(host="127.0.0.1"), _service())
    try:
        assert not isinstance(server.socket, ssl.SSLSocket)
    finally:
        server.server_close()


def _self_signed(tmp_path: Path, key_mode: int = 0o600) -> tuple[Path, Path]:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    key.chmod(key_mode)
    return cert, key


def test_build_server_rejects_group_readable_tls_key(tmp_path):
    cert, key = _self_signed(tmp_path, key_mode=0o640)
    with pytest.raises(SystemExit, match="mode 600"):
        build_server(_args(host="0.0.0.0", tls_cert=str(cert), tls_key=str(key)), _service())


def test_main_fails_loudly_when_cli_missing_from_path(tmp_path, monkeypatch):
    token = _write_token(tmp_path / "token")
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing resolvable here
    with pytest.raises(SystemExit, match="Codex CLI not found"):
        bridge.main(["--token-file", str(token), "--codex-bin", "codex", "--claude-bin", "claude"])


class _FakeService:
    """Stand-in that never launches CLIs but honours the real auth contract."""

    token = "s" * 64

    class sessions:  # noqa: N801 - mimics _Service.sessions
        @staticmethod
        def names():
            return ["default", "work"]

    def get(self, path, session="default"):
        if session not in ("default", "work"):
            raise bridge.SessionError(session)
        if path == "/v1/codex/rate-limits":
            return {"authenticated": True, "rate_limits": [], "session": session}
        if path == "/v1/claude/usage":
            raise bridge.BridgeError("boom")
        if path == "/v1/explode":
            raise RuntimeError("unexpected")
        raise KeyError(path)


def _serve(tmp_path: Path):
    cert, key = _self_signed(tmp_path)
    server = build_server(_args(host="127.0.0.1", tls_cert=str(cert), tls_key=str(key)), _FakeService())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = ssl.create_default_context(cafile=str(cert))
    return server, context


def _get(server, context, path, headers=None):
    conn = http.client.HTTPSConnection("127.0.0.1", server.server_address[1], context=context, timeout=5)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read().decode("utf-8"))
    finally:
        conn.close()


def test_tls_server_authorizes_only_exact_bearer_token(tmp_path):
    server, context = _serve(tmp_path)
    try:
        good = {"Authorization": "Bearer " + _FakeService.token}
        assert _get(server, context, "/health")[0] == 200
        assert _get(server, context, "/v1/codex/rate-limits")[0] == 401
        assert _get(server, context, "/v1/codex/rate-limits", {"Authorization": "Bearer wrong"})[0] == 401
        assert _get(server, context, "/v1/codex/rate-limits", {"Authorization": "Basic abc"})[0] == 401
        assert _get(server, context, "/v1/codex/rate-limits", {"Authorization": "bearer " + _FakeService.token})[0] == 401
        assert _get(server, context, "/v1/codex/rate-limits", good) == (200, {"authenticated": True, "rate_limits": [], "session": "default"})
        assert _get(server, context, "/v1/codex/rate-limits?session=work", good)[1]["session"] == "work"
        assert _get(server, context, "/v1/codex/rate-limits?session=nope", good) == (404, {"error": "unknown_session"})
        assert _get(server, context, "/v1/codex/rate-limits?session=a&session=b", good) == (400, {"error": "bad_session"})
        assert _get(server, context, "/v1/codex/rate-limits?session=", good) == (404, {"error": "unknown_session"})
        for bad in ("..%2Fwork", "%2e%2e", "work%00", "WORK", "work%2F", "a%0Ab"):
            assert _get(server, context, f"/v1/codex/rate-limits?session={bad}", good)[0] == 404, bad
        assert _get(server, context, "/v1/sessions", good) == (200, {"sessions": ["default", "work"]})
        assert _get(server, context, "/v1/sessions")[0] == 401
        assert _get(server, context, "/v1/claude/usage", good) == (502, {"error": "oauth_upstream_unavailable"})
        assert _get(server, context, "/v1/explode", good) == (500, {"error": "bridge_failure"})
        assert _get(server, context, "/v1/nope", good) == (404, {"error": "not_found"})
    finally:
        server.shutdown()
        server.server_close()


def test_tls_server_rejects_plaintext_clients(tmp_path):
    server, _ = _serve(tmp_path)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        with pytest.raises((http.client.HTTPException, ConnectionError, socket.timeout, OSError)):
            conn.request("GET", "/health")
            conn.getresponse()
    finally:
        server.shutdown()
        server.server_close()


def test_idle_tcp_client_does_not_block_other_tls_clients(tmp_path):
    """One connection that never sends a ClientHello must not stall accept()."""
    server, context = _serve(tmp_path)
    idle = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
    try:
        # With the handshake on the accept thread this call timed out; now the
        # idle socket is parked in its own handler thread and we get served.
        status, body = _get(server, context, "/health")
        assert status == 200 and body["status"] == "ok"
    finally:
        idle.close()
        server.shutdown()
        server.server_close()


def test_handshake_timeout_drops_idle_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge._TlsServer, "handshake_timeout", 0.5)
    server, context = _serve(tmp_path)
    idle = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
    try:
        idle.settimeout(3)
        # Server should close the never-handshaking socket after the timeout.
        assert idle.recv(1) == b""
        assert _get(server, context, "/health")[0] == 200
    finally:
        idle.close()
        server.shutdown()
        server.server_close()


# --- multi-account sessions -------------------------------------------------


def _sessions(tmp_path: Path) -> bridge._Sessions:
    root = tmp_path / "sessions"
    (root / "work" / "codex").mkdir(parents=True)
    (root / "work" / "claude").mkdir(parents=True)
    (root / "codex-only" / "codex").mkdir(parents=True)
    (root / "Bad Name" / "codex").mkdir(parents=True)  # must be ignored/rejected
    return bridge._Sessions(root, base_env={"PATH": "/usr/bin"}, default_projects_dir=tmp_path / "default-projects")


def test_default_session_uses_normal_cli_homes(tmp_path):
    s = _sessions(tmp_path)
    assert "CODEX_HOME" not in s.codex_env("default")
    env, projects = s.claude_env("default")
    assert "CLAUDE_CONFIG_DIR" not in env and projects == tmp_path / "default-projects"


def test_named_session_isolates_cli_homes(tmp_path):
    s = _sessions(tmp_path)
    assert s.codex_env("work")["CODEX_HOME"] == str(tmp_path / "sessions" / "work" / "codex")
    env, projects = s.claude_env("work")
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "sessions" / "work" / "claude")
    assert projects == tmp_path / "sessions" / "work" / "claude" / "projects"
    assert env["PATH"] == "/usr/bin"  # base env preserved


@pytest.mark.parametrize("name", ["../etc", "..", "work/../default", "Work", "a b", "x" * 33, "-lead", "", "work\x00"])
def test_session_names_that_could_escape_or_collide_are_rejected(tmp_path, name):
    s = _sessions(tmp_path)
    with pytest.raises(bridge.SessionError):
        s.codex_env(name)


def test_session_without_tool_dir_is_unknown_for_that_tool(tmp_path):
    s = _sessions(tmp_path)
    assert "CODEX_HOME" in s.codex_env("codex-only")
    with pytest.raises(bridge.SessionError):
        s.claude_env("codex-only")
    with pytest.raises(bridge.SessionError):
        s.codex_env("missing")


def test_session_listing_excludes_invalid_names(tmp_path):
    assert _sessions(tmp_path).names() == ["default", "codex-only", "work"]


# --- capture (logout/login flow) -------------------------------------------


def _fake_cli_homes(tmp_path: Path) -> tuple[Path, Path]:
    codex = tmp_path / "codex-home"
    claude = tmp_path / "claude-home"
    codex.mkdir()
    claude.mkdir()
    (codex / "auth.json").write_text('{"tokens":{"access_token":"cx-secret"}}')
    (codex / "config.toml").write_text("model='x'")
    (codex / "memories_1.sqlite").write_bytes(b"\x00" * 16)
    (claude / ".credentials.json").write_text('{"claudeAiOauth":{"accessToken":"cl-secret"}}')
    (claude / "projects").mkdir()
    (claude / "projects" / "transcript.jsonl").write_text("{}\n")
    return codex, claude


def test_capture_copies_only_credential_files(tmp_path):
    codex, claude = _fake_cli_homes(tmp_path)
    sessions = tmp_path / "sessions"

    result = bridge.capture_session("work", sessions, codex_home=codex, claude_config_dir=claude)

    assert result == {"codex": "captured", "claude": "captured"}
    assert sorted(p.name for p in (sessions / "work" / "codex").iterdir()) == ["auth.json"]
    assert sorted(p.name for p in (sessions / "work" / "claude").iterdir()) == [".credentials.json"]
    assert (sessions / "work" / "codex" / "auth.json").read_text() == '{"tokens":{"access_token":"cx-secret"}}'
    # And the captured session is immediately usable by the bridge.
    s = bridge._Sessions(sessions, base_env={}, default_projects_dir=tmp_path)
    assert s.codex_env("work")["CODEX_HOME"] == str(sessions / "work" / "codex")


def test_capture_writes_private_files_and_dirs(tmp_path):
    codex, claude = _fake_cli_homes(tmp_path)
    sessions = tmp_path / "sessions"
    bridge.capture_session("work", sessions, codex_home=codex, claude_config_dir=claude)
    for p in [sessions, sessions / "work", sessions / "work" / "codex", sessions / "work" / "claude"]:
        assert stat.S_IMODE(p.stat().st_mode) == 0o700
    for p in [sessions / "work" / "codex" / "auth.json", sessions / "work" / "claude" / ".credentials.json"]:
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_capture_recapture_overwrites_atomically(tmp_path):
    codex, claude = _fake_cli_homes(tmp_path)
    sessions = tmp_path / "sessions"
    bridge.capture_session("work", sessions, codex_home=codex, claude_config_dir=claude)
    (codex / "auth.json").write_text('{"tokens":{"access_token":"cx-NEW"}}')
    bridge.capture_session("work", sessions, codex_home=codex, claude_config_dir=claude)
    assert (sessions / "work" / "codex" / "auth.json").read_text() == '{"tokens":{"access_token":"cx-NEW"}}'
    assert not (sessions / "work" / "codex" / "auth.json.tmp").exists()


def test_capture_reports_tools_that_are_not_signed_in(tmp_path):
    codex, claude = _fake_cli_homes(tmp_path)
    (claude / ".credentials.json").unlink()
    result = bridge.capture_session("work", tmp_path / "sessions", codex_home=codex, claude_config_dir=claude)
    assert result == {"codex": "captured", "claude": "not signed in"}
    assert not (tmp_path / "sessions" / "work" / "claude").exists()


def test_capture_only_selected_tool(tmp_path):
    codex, claude = _fake_cli_homes(tmp_path)
    result = bridge.capture_session("work", tmp_path / "sessions", tools=("claude",), codex_home=codex, claude_config_dir=claude)
    assert result == {"claude": "captured"}
    assert not (tmp_path / "sessions" / "work" / "codex").exists()


@pytest.mark.parametrize("name", ["default", "../x", "Work", "", "a b"])
def test_capture_rejects_bad_session_names(tmp_path, name):
    codex, claude = _fake_cli_homes(tmp_path)
    with pytest.raises(SystemExit, match="invalid session name"):
        bridge.capture_session(name, tmp_path / "sessions", codex_home=codex, claude_config_dir=claude)
    assert not (tmp_path / "sessions").exists()


def test_capture_does_not_write_through_preplanted_tmp_symlink(tmp_path):
    """Reviewer probe: sessions/work/codex/auth.json.tmp -> victim must not receive credentials."""
    codex, claude = _fake_cli_homes(tmp_path)
    sessions = tmp_path / "sessions"
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    (sessions / "work" / "codex").mkdir(parents=True)
    (sessions / "work" / "codex" / "auth.json.tmp").symlink_to(victim)
    (sessions / "work" / "codex" / "auth.json").symlink_to(victim)

    bridge.capture_session("work", sessions, tools=("codex",), codex_home=codex, claude_config_dir=claude)

    assert victim.read_text() == "untouched"
    dst = sessions / "work" / "codex" / "auth.json"
    assert not dst.is_symlink() and "cx-secret" in dst.read_text()


def test_capture_refuses_symlinked_session_dir(tmp_path):
    codex, claude = _fake_cli_homes(tmp_path)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (sessions / "evil").symlink_to(elsewhere)
    with pytest.raises(SystemExit, match="symlinked session"):
        bridge.capture_session("evil", sessions, codex_home=codex, claude_config_dir=claude)
    assert list(elsewhere.iterdir()) == []


def test_symlinked_session_dirs_are_neither_listed_nor_usable(tmp_path):
    s = _sessions(tmp_path)
    outside = tmp_path / "outside" / "codex"
    outside.mkdir(parents=True)
    (s.sessions_dir / "linked").symlink_to(tmp_path / "outside")
    (s.sessions_dir / "work" / "claude").rmdir()
    (s.sessions_dir / "work" / "claude").symlink_to(outside)
    assert "linked" not in s.names()
    with pytest.raises(bridge.SessionError):
        s.codex_env("linked")
    with pytest.raises(bridge.SessionError):
        s.claude_env("work")  # tool dir itself is a symlink
    assert "CODEX_HOME" in s.codex_env("work")  # real sibling dir still fine


def test_capture_cli_entrypoint(tmp_path, monkeypatch, capsys):
    codex, claude = _fake_cli_homes(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    rc = bridge.main(["capture", "personal", "--sessions-dir", str(tmp_path / "sessions")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "codex: captured" in out and "claude: captured" in out and "oauth_session: personal" in out
    assert "cx-secret" not in out and "cl-secret" not in out

    (codex / "auth.json").unlink()
    (claude / ".credentials.json").unlink()
    rc = bridge.main(["capture", "other", "--sessions-dir", str(tmp_path / "sessions")])
    assert rc == 1
    assert "nothing captured" in capsys.readouterr().out


# --- app-server transport ---------------------------------------------------


def test_service_locks_are_scoped_per_tool_and_session(tmp_path, monkeypatch):
    """Two different sessions must not serialize on each other (CodeRabbit finding)."""
    root = tmp_path / "sessions"
    for name in ("a", "b"):
        (root / name / "codex").mkdir(parents=True)
    svc = bridge._Service("t" * 64, codex_bin="/bin/true", claude_bin="/bin/true", projects_dir=tmp_path, timeout=5, sessions_dir=root)

    started = threading.Barrier(3, timeout=5)  # two workers + the test thread
    release = threading.Event()

    def slow_rate_limits(self):
        started.wait()  # both calls must be inside get() concurrently
        release.wait(5)
        return {"authenticated": True, "rate_limits": []}

    monkeypatch.setattr(bridge._AppServerClient, "rate_limits", slow_rate_limits)
    results = []
    threads = [threading.Thread(target=lambda s: results.append(svc.get("/v1/codex/rate-limits", s)), args=(s,)) for s in ("a", "b")]
    for t in threads:
        t.start()
    started.wait()  # would raise BrokenBarrierError if a global lock serialized them
    release.set()
    for t in threads:
        t.join(5)
    assert len(results) == 2

    # Same session IS serialized: second caller cannot enter until first exits.
    lock = svc._lock_for("codex", "a")
    assert lock is svc._lock_for("codex", "a") and lock is not svc._lock_for("codex", "b") and lock is not svc._lock_for("claude", "a")


def test_app_server_reader_handles_partial_lines_and_noise(tmp_path):
    script = tmp_path / "fake_app_server.py"
    script.write_text(
        "import sys, time\n"
        "sys.stdin.readline()\n"
        "sys.stdout.write('{\"id\":1,\"result\":{}}\\n'); sys.stdout.flush()\n"
        "sys.stdin.readline(); sys.stdin.readline()\n"
        "sys.stdout.write('not json\\n'); sys.stdout.flush()\n"
        "sys.stdout.write('{\"id\":2,\"result\":{\"rateLimits\":{\"primary\":{\"usedPercent\":5'); sys.stdout.flush()\n"
        "time.sleep(0.3)\n"
        "sys.stdout.write('}},\"accessToken\":\"leak\"}}\\n'); sys.stdout.flush()\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )
    client = bridge._AppServerClient([sys.executable, str(script)], timeout=5)
    result = client.rate_limits()
    assert result["authenticated"] is True
    assert result["rate_limits"] == [{"name": "primary", "used_percent": 5.0, "remaining_percent": 95.0}]
    assert "leak" not in json.dumps(result)


def test_app_server_reader_times_out_on_partial_line(tmp_path):
    script = tmp_path / "hang.py"
    script.write_text(
        "import sys, time\n"
        "sys.stdin.readline()\n"
        "sys.stdout.write('{\"id\":1,\"result\":{}}\\n'); sys.stdout.flush()\n"
        "sys.stdin.readline(); sys.stdin.readline()\n"
        "sys.stdout.write('{\"id\":2,'); sys.stdout.flush()\n"  # never completes the line
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    client = bridge._AppServerClient([sys.executable, str(script)], timeout=1)
    t = time.monotonic()
    with pytest.raises(bridge.BridgeError, match="timed out"):
        client.rate_limits()
    assert time.monotonic() - t < 5
