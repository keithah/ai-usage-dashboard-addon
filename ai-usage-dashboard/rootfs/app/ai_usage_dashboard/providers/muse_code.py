"""Muse Code adapter: no live quota polling.

Authoritative findings: the official docs provide ``muse exec --json``
and Meta API inference/rate-limit headers, but no documented subscription
remaining-quota endpoint or schema. ``muse exec --json`` is not run merely
to estimate subscription quota, web pages are never scraped, and no
undocumented account API is called (the direct POST to
``api.meta.ai/muse-code/key`` and all ``subs_usage`` assumptions were
removed for exactly that reason).

Collection therefore always reports ``unsupported`` with a clear reason.
Auth semantics stay honest: a ``META_API_KEY`` value is pay-as-you-go API
access, not proof of subscription quota; subscription use is through the
signed-in Muse CLI. Manage the subscription in the official Meta/Muse
web console. Provider registration and config-only account additions are
preserved: an account reports ``unsupported`` with diagnostics instead of
a faked zero.

Legacy ``key_url``/``status_url``/``auth_mode`` options were removed with
the undocumented endpoint and are rejected as config errors with
migration guidance.
"""
from __future__ import annotations

from ..models import AccountConfig, AccountSnapshot, SnapshotStatus
from .base import CollectContext
from ._helpers import terminal

provider_name = "muse_code"

_LEGACY_OPTIONS = ("key_url", "status_url", "auth_mode")


class Adapter:
    provider_name = "muse_code"

    def collect(self, account: AccountConfig, ctx: CollectContext) -> AccountSnapshot:
        legacy = [k for k in _LEGACY_OPTIONS if k in account.options]
        if legacy:
            return terminal(
                account, SnapshotStatus.ERROR,
                "Muse Code no longer posts to a key/quota endpoint "
                f"({', '.join(sorted(legacy))} were removed because no "
                "subscription remaining-quota endpoint is documented); "
                "remove those options. `muse exec --json` is not run to "
                "estimate quota, and META_API_KEY is pay-as-you-go API "
                "access, not proof of subscription quota.",
            )
        if ctx.fixture_mode:
            error = (ctx.account_fixture or {}).get("error")
            if error:
                return _snapshot_from_error(account, error)
        return terminal(
            account, SnapshotStatus.UNSUPPORTED,
            "Muse Code subscription_quota is unavailable: no documented "
            "subscription remaining-quota endpoint or schema exists; "
            "`muse exec --json` is not run merely to estimate quota and no "
            "undocumented account API is called. A META_API_KEY value is "
            "pay-as-you-go API access, not proof of subscription quota "
            "(subscription use is through the signed-in Muse CLI); manage "
            "the subscription in the official Meta/Muse web console.",
        )

    def fixture_names(self) -> list[str]:
        return []


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
