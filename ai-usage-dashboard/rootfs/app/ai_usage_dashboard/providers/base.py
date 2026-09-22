"""Provider adapter interface."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..http_client import SafeHttpClient
from ..models import AccountConfig, AccountSnapshot


@dataclass
class CollectContext:
    """Per-run context handed to adapters."""

    http: SafeHttpClient
    fixture_mode: bool = False
    fixtures: dict = field(default_factory=dict)
    # fixture payload for THIS account, when fixture_mode is on:
    # {"responses": {name: body}, "headers": {...}, "error": {...}}
    account_fixture: dict = field(default_factory=dict)


class ProviderAdapter(Protocol):
    provider_name: str

    def collect(
        self, account: AccountConfig, ctx: CollectContext
    ) -> AccountSnapshot:
        ...
