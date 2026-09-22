"""Provider registry."""
from __future__ import annotations

from . import alibaba_coding_plan, anthropic, deepseek, kimi, muse_code, opencode_go, openai

_ADAPTERS = {
    "openai": openai.Adapter,
    "anthropic": anthropic.Adapter,
    "kimi": kimi.Adapter,
    "deepseek": deepseek.Adapter,
    "opencode_go": opencode_go.Adapter,
    "muse_code": muse_code.Adapter,
    "alibaba_coding_plan": alibaba_coding_plan.Adapter,
}


def get_adapter(provider: str):
    try:
        factory = _ADAPTERS[provider]
    except KeyError:
        raise KeyError(f"unknown provider {provider!r}") from None
    return factory()


def providers() -> list[str]:
    return sorted(_ADAPTERS)
