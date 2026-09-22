"""Collector: run all account adapters, preserve last valid values on
transient failures (stale), never mix currencies."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from . import providers as provider_registry
from .http_client import HttpRateLimitError, HttpTransientError
from .models import AccountConfig, AccountSnapshot, SnapshotStatus
from .providers.base import CollectContext
from .providers.openai import _Transient as FixtureTransient


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, (HttpTransientError, HttpRateLimitError, FixtureTransient))


class Collector:
    def __init__(
        self,
        http,
        fixtures: dict | None = None,
        fixture_mode: bool = False,
        state_path: str | None = None,
    ):
        self.http = http
        self.fixtures = fixtures or {}
        self.fixture_mode = fixture_mode
        self.state_path = state_path
        self._last_valid: dict[str, dict] = {}
        if state_path and os.path.exists(state_path):
            try:
                with open(state_path, encoding="utf-8") as fh:
                    self._last_valid = json.load(fh)
            except (OSError, ValueError):
                self._last_valid = {}

    def collect_all(self, accounts: list[AccountConfig]) -> list[AccountSnapshot]:
        snapshots = [self.collect_one(a) for a in accounts]
        self._persist()
        return snapshots

    def collect_one(self, account: AccountConfig) -> AccountSnapshot:
        adapter = provider_registry.get_adapter(account.provider)
        ctx = CollectContext(
            http=self.http,
            fixture_mode=self.fixture_mode,
            fixtures=self.fixtures,
            account_fixture=self.fixtures.get(account.key, {}),
        )
        try:
            snap = adapter.collect(account, ctx)
        except Exception as exc:
            if _is_transient(exc):
                return self._stale_from_last(account, str(exc))
            return AccountSnapshot(
                provider=account.provider,
                account_id=account.account_id,
                display_name=account.display_name,
                status=SnapshotStatus.ERROR,
                reason=f"{type(exc).__name__}: {exc}",
                metrics=[],
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )
        if snap.status == SnapshotStatus.FRESH:
            self._last_valid[account.key] = snap.to_dict()
        elif snap.status in (SnapshotStatus.STALE,) or _snapshot_is_transient_error(
            snap
        ):
            stale = self._stale_from_last(account, snap.reason)
            if stale.metrics:
                return stale
        return snap

    def _stale_from_last(self, account: AccountConfig, reason: str) -> AccountSnapshot:
        from .models import Metric, Unit, Window

        last = self._last_valid.get(account.key)
        if not last:
            return AccountSnapshot(
                provider=account.provider,
                account_id=account.account_id,
                display_name=account.display_name,
                status=SnapshotStatus.ERROR,
                reason=f"transient failure with no prior valid snapshot: {reason}",
                metrics=[],
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )
        metrics = []
        for raw in last.get("metrics", []):
            window = None
            if raw.get("window"):
                window = Window(
                    kind=raw["window"].get("kind", ""),
                    label=raw["window"].get("label", ""),
                    start=raw["window"].get("start"),
                    end=raw["window"].get("end"),
                )
            metrics.append(
                Metric(
                    key=raw["key"],
                    label=raw["label"],
                    value=raw["value"],
                    unit=Unit(raw["unit"]),
                    window=window,
                )
            )
        return AccountSnapshot(
            provider=account.provider,
            account_id=account.account_id,
            display_name=account.display_name,
            status=SnapshotStatus.STALE,
            reason=f"transient failure; showing last valid values: {reason}",
            metrics=metrics,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )

    def _persist(self) -> None:
        if not self.state_path:
            return
        try:
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump(self._last_valid, fh, indent=2)
        except OSError:
            pass


def _snapshot_is_transient_error(snap: AccountSnapshot) -> bool:
    return snap.status == SnapshotStatus.ERROR and "transient" in snap.reason.lower()
