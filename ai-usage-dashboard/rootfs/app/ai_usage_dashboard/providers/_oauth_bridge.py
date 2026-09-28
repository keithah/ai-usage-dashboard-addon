"""Shared helpers for adapters that talk to the host-side OAuth metrics bridge."""
from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import quote, urlsplit

from ..models import AccountConfig, AccountSnapshot, SnapshotStatus
from ._helpers import _Auth, classify_http, resolve_live_credential, terminal
from .base import CollectContext

_NAME_RE = re.compile(r"[^a-z0-9]+")
_SESSION_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class BridgeConfigError(ValueError):
    """The account's OAuth bridge settings are invalid (never a scrape)."""


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def bridge_endpoint(account: AccountConfig, path: str) -> str:
    """Build a bridge URL, rejecting plaintext transport off the loopback host.

    The bridge bearer token travels in the Authorization header, so an http://
    URL to a LAN address would expose it to sniffing and replay.
    """
    base = str(account.options.get("oauth_bridge_url", "") or "").strip()
    if not base:
        raise BridgeConfigError("oauth_bridge_url is required for OAuth providers")
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise BridgeConfigError("oauth_bridge_url must be an http(s) URL")
    if parsed.path.strip("/") or parsed.query or parsed.fragment:
        raise BridgeConfigError("oauth_bridge_url must be just scheme://host[:port] with no path or query")
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        raise BridgeConfigError(
            "oauth_bridge_url must use https:// unless it points at localhost; "
            "the bridge token is sent as a bearer header"
        )
    return base.rstrip("/") + path


def bridge_session(account: AccountConfig) -> str:
    """Validated session name; add-on side mirrors the bridge's strict pattern."""
    raw = str(account.options.get("oauth_session", "") or "").strip()
    if not raw:
        return "default"
    if not _SESSION_RE.fullmatch(raw):
        raise BridgeConfigError(
            "oauth_session must be 1-32 chars of lowercase letters, digits, '-' or '_'"
        )
    return raw


def bridge_ca_file(account: AccountConfig) -> str | None:
    value = account.options.get("oauth_bridge_ca_file")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def fetch_bridge_document(
    account: AccountConfig, ctx: CollectContext, path: str
) -> dict[str, Any] | AccountSnapshot:
    """Return the bridge JSON document, or a terminal snapshot on failure."""
    try:
        token = resolve_live_credential(account, ctx)
    except _Auth as exc:
        return terminal(account, SnapshotStatus.AUTH_ERROR, str(exc))
    try:
        url = bridge_endpoint(account, path)
        session = bridge_session(account)
    except BridgeConfigError as exc:
        return terminal(account, SnapshotStatus.ERROR, str(exc))
    if session != "default":
        url = f"{url}?session={quote(session, safe='')}"
    headers = {"Authorization": f"Bearer {token}"}
    ca_file = bridge_ca_file(account)
    try:
        response = ctx.http.get(url, headers=headers, ca_file=ca_file)
    except Exception as exc:  # classify_http maps every client failure
        return classify_http(account, exc)
    body = response.body
    return body if isinstance(body, dict) else {}


def metric_name(raw: Any, default: str = "window") -> str:
    """Normalize a bridge-supplied name into a safe metric/entity key fragment."""
    text = _NAME_RE.sub("_", str(raw if raw is not None else "").lower()).strip("_")
    return text or default
