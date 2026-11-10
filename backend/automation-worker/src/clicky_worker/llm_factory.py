"""Build the browser-use chat model from the backend's AgentLLMConfig.

API keys come from this process's environment (the backend passes only the one the provider needs), never over IPC.

- anthropic: ChatAnthropic with adaptive thinking. Claude Opus 5.5 rejects forced tool_choice with a 400, and
  browser-use only switches to `tool_choice: auto` when thinking is set; adaptive is also the only thinking mode it
  accepts. `effort` (default "low") goes to output_config: the agent takes many small steps.
- openai_compat: ChatOpenAI against the configured base URL.
- ollama: ChatOllama with the same runner options (num_ctx, ...) as every other role on that model, so the model is
  not reloaded, and think off. Experimental: small models often break the action schema.
"""

from __future__ import annotations

import os
from typing import Any

from clicky_worker.ipc import AgentLLMConfig

DEFAULT_LLM_TIMEOUT_S = 120


def build_llm(config: AgentLLMConfig) -> Any:
    options = dict(config.options)
    if config.provider == "anthropic":
        from browser_use import ChatAnthropic

        effort = options.get("effort", "low")
        return ChatAnthropic(
            model=config.model,
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            max_tokens=int(options.get("max_tokens") or 16_000),   # thinking counts toward it
            timeout=float(options.get("timeout_s", DEFAULT_LLM_TIMEOUT_S)),
            max_retries=3,
        )
    if config.provider == "openai_compat":
        from browser_use import ChatOpenAI

        return ChatOpenAI(model=config.model, api_key=os.environ.get("OPENAI_API_KEY"), base_url=config.base_url,
                          timeout=float(options.get("timeout_s", DEFAULT_LLM_TIMEOUT_S)), max_retries=3)
    if config.provider == "ollama":
        from browser_use import ChatOllama

        runner = dict(options.get("runner") or {})
        # think / keep_alive are passed through to the top level of /api/chat by ChatOllama
        return ChatOllama(model=config.model, host=config.base_url,
                          timeout=float(options.get("timeout_s", 300)),
                          ollama_options={**runner, "temperature": options.get("temperature", 0.2),
                                          "think": bool(options.get("think", False)),
                                          "keep_alive": options.get("keep_alive", "30m")})
    raise ValueError(f"no browser-use model for provider {config.provider!r}")


def agent_settings(config: AgentLLMConfig) -> dict[str, Any]:
    """Agent(...) keyword arguments that depend on the model."""
    if config.provider == "ollama":
        # Small local models: fewer actions per step, no planning, a long per-call timeout, DOM only by default.
        options = config.options
        return {"flash_mode": bool(options.get("flash_mode", True)), "max_actions_per_step": 2,
                "use_vision": bool(options.get("use_vision", False)), "llm_timeout": 300, "enable_planning": False}
    return {"llm_timeout": DEFAULT_LLM_TIMEOUT_S, "max_actions_per_step": 4}
