"""Safe HTTP client: timeouts, bounded retries/backoff, redacted errors."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


class HttpError(Exception):
    """Base HTTP failure with all secrets redacted."""

    def __init__(self, message: str, *, status: int | None = None, retriable: bool = False):
        super().__init__(message)
        self.status = status
        self.retriable = retriable


class HttpAuthError(HttpError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message, status=status, retriable=False)


class HttpRateLimitError(HttpError):
    def __init__(self, message: str, *, status: int = 429, retry_after: float | None = None):
        super().__init__(message, status=status, retriable=True)
        self.retry_after = retry_after


class HttpTransientError(HttpError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message, status=status, retriable=True)


@dataclass
class HttpResponse:
    status: int
    headers: dict
    body: dict | list | None

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


def _redact_headers(headers: dict) -> dict:
    redacted = {}
    for key, value in headers.items():
        if key.lower() in ("authorization", "x-api-key", "api-key", "cookie"):
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted


@dataclass
class SafeHttpClient:
    """urllib-based client. Pass a fake `handler` in tests to avoid network."""

    timeout: float = 15.0
    max_retries: int = 2
    backoff_base: float = 1.0
    handler: object = None  # callable(method, url, headers, timeout) -> HttpResponse
    sleep: object = field(default=time.sleep, repr=False)

    def get(self, url: str, headers: dict | None = None) -> HttpResponse:
        return self.request("GET", url, headers=headers)

    def request(self, method: str, url: str, headers: dict | None = None) -> HttpResponse:
        safe_headers = _redact_headers(headers or {})
        attempts = 0
        while True:
            attempts += 1
            try:
                if self.handler is not None:
                    resp = self.handler(method, url, headers or {}, self.timeout)  # type: ignore[operator]
                else:
                    resp = self._perform(method, url, headers or {})
            except (TimeoutError, ConnectionError, OSError) as exc:
                if attempts > self.max_retries:
                    raise HttpTransientError(
                        f"{method} {url} failed after {attempts} attempts: "
                        f"{type(exc).__name__}"
                    ) from None
                self.sleep(self.backoff_base * (2 ** (attempts - 1)))
                continue
            if resp.status in (401, 403):
                raise HttpAuthError(
                    f"{method} {url} -> HTTP {resp.status} "
                    f"(headers={_redact_headers(dict(resp.headers))}); "
                    "check credential permissions"
                , status=resp.status)
            if resp.status == 429:
                retry_after = None
                raw = resp.header("retry-after") if hasattr(resp, "header") else None
                try:
                    retry_after = float(raw) if raw else None
                except (TypeError, ValueError):
                    retry_after = None
                if attempts > self.max_retries:
                    raise HttpRateLimitError(
                        f"{method} {url} -> HTTP 429 rate limited; giving up",
                        retry_after=retry_after,
                    )
                self.sleep(retry_after or self.backoff_base * (2 ** (attempts - 1)))
                continue
            if 500 <= resp.status < 600:
                if attempts > self.max_retries:
                    raise HttpTransientError(
                        f"{method} {url} -> HTTP {resp.status} "
                        "after retries; upstream unavailable",
                        status=resp.status,
                    )
                self.sleep(self.backoff_base * (2 ** (attempts - 1)))
                continue
            if 400 <= resp.status < 600:
                raise HttpError(
                    f"{method} {url} -> HTTP {resp.status}",
                    status=resp.status,
                    retriable=False,
                )
            _ = safe_headers
            return resp

    def _perform(self, method: str, url: str, headers: dict) -> HttpResponse:
        req = urllib.request.Request(url, method=method, headers=dict(headers))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as fh:
                status = getattr(fh, "status", 200)
                raw_headers = dict(fh.headers.items()) if fh.headers else {}
                payload = fh.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw_headers = dict(exc.headers.items()) if exc.headers else {}
            try:
                payload = exc.read()
            except Exception:
                payload = b""
            # Never include response bodies in errors (may contain account data).
            if status in (401, 403):
                raise HttpAuthError(
                    f"{method} {url} -> HTTP {status}; check credential permissions",
                    status=status,
                ) from None
            if status == 429:
                raise HttpRateLimitError(
                    f"{method} {url} -> HTTP 429 rate limited", status=status
                ) from None
            if 500 <= status < 600:
                raise HttpTransientError(
                    f"{method} {url} -> HTTP {status}; upstream unavailable",
                    status=status,
                ) from None
            raise HttpError(f"{method} {url} -> HTTP {status}", status=status) from None
        except TimeoutError as exc:
            raise HttpTransientError(
                f"{method} {url} timed out after {self.timeout}s"
            ) from None
        except (ConnectionError, OSError) as exc:
            raise HttpTransientError(
                f"{method} {url} connection failed: {type(exc).__name__}"
            ) from None
        try:
            body = json.loads(payload.decode("utf-8")) if payload else None
        except (ValueError, UnicodeDecodeError):
            raise HttpError(
                f"{method} {url} -> HTTP {status} with non-JSON body",
                status=status,
            ) from None
        return HttpResponse(status=status, headers=raw_headers, body=body)
