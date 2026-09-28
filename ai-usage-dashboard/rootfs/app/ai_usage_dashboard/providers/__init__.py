"""Provider registry."""
from __future__ import annotations

from . import alibaba_coding_plan, anthropic, claude_code_oauth, codex_oauth, coderabbit, deepseek, gemini, grok, kimi, muse_code, opencode_go, openai, openrouter

_ADAPTERS = {
    "openai": openai.Adapter,
    "anthropic": anthropic.Adapter,
    "codex_oauth": codex_oauth.Adapter,
    "claude_code_oauth": claude_code_oauth.Adapter,
    "kimi": kimi.Adapter,
    "deepseek": deepseek.Adapter,
    "opencode_go": opencode_go.Adapter,
    "muse_code": muse_code.Adapter,
    "alibaba_coding_plan": alibaba_coding_plan.Adapter,
    "grok": grok.Adapter,
    "openrouter": openrouter.Adapter,
    "gemini": gemini.Adapter,
    "coderabbit": coderabbit.Adapter,
}


def get_adapter(provider: str):
    try:
        factory = _ADAPTERS[provider]
    except KeyError:
        raise KeyError(f"unknown provider {provider!r}") from None
    return factory()


def providers() -> list[str]:
    return sorted(_ADAPTERS)
