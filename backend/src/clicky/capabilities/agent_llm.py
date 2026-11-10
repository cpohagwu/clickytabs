"""agent_llm descriptors: the LLM the browser-use automation worker should build (no in-process inference).

`worker_config()` becomes the worker's `AgentLLMConfig` (provider, model, base_url, options) plus the child-process
environment holding the API key; keys never travel over IPC and are never logged.
"""

from __future__ import annotations

from clicky.capabilities.base import AgentLLMConfigOut, Health, ProviderSpec
from clicky.capabilities.context import ProviderContext
from clicky.capabilities.ollama_api import OllamaError
from clicky.capabilities.options import AnthropicAgentOptions, OllamaAgentOptions, OpenAICompatAgentOptions


class _AgentDescriptor:
    def __init__(self, spec: ProviderSpec, *, model: str, context: ProviderContext) -> None:
        self.spec = spec
        self.model = model
        self._ctx = context

    async def health(self) -> Health:
        """The registry has already checked the API key; there is nothing to load in-process."""
        return Health(True)

    async def aclose(self) -> None:
        return None

    def _secret_env(self, env_name: str) -> dict[str, str]:
        value = self._ctx.settings.secret_value(self.spec.secret)
        return {env_name: value} if value else {}


class AnthropicAgentLLM(_AgentDescriptor):
    def __init__(self, spec: ProviderSpec, *, model: str, options: AnthropicAgentOptions,
                 context: ProviderContext) -> None:
        super().__init__(spec, model=model, context=context)
        self.options = options

    def worker_config(self) -> AgentLLMConfigOut:
        return AgentLLMConfigOut(provider="anthropic", model=self.model,
                                 options=self.options.model_dump(exclude_none=True),
                                 env=self._secret_env("ANTHROPIC_API_KEY"))


class OpenAICompatAgentLLM(_AgentDescriptor):
    def __init__(self, spec: ProviderSpec, *, model: str, options: OpenAICompatAgentOptions,
                 context: ProviderContext) -> None:
        super().__init__(spec, model=model, context=context)
        self.options = options

    def worker_config(self) -> AgentLLMConfigOut:
        options = self.options.model_dump(exclude_none=True, exclude={"base_url"})
        return AgentLLMConfigOut(provider="openai_compat", model=self.model,
                                 base_url=self.options.base_url or self._ctx.settings.openai_base_url,
                                 options=options, env=self._secret_env("OPENAI_API_KEY"))


class OllamaAgentLLM(_AgentDescriptor):
    def __init__(self, spec: ProviderSpec, *, model: str, options: OllamaAgentOptions,
                 context: ProviderContext) -> None:
        super().__init__(spec, model=model, context=context)
        self.options = options

    async def health(self) -> Health:
        try:
            if not await self._ctx.ollama.has_model(self.model):
                return Health(False, f"{self.model} is not pulled", self._ctx.pull_hint(self.model))
        except OllamaError as exc:
            return Health(False, str(exc), "start Ollama, or set [ollama] host in clicky.toml / OLLAMA_HOST")
        return Health(True, "experimental: small local models may break the browser-use action schema")

    def worker_config(self) -> AgentLLMConfigOut:
        """Same runner options as every other role on this model, so the worker never forces a reload."""
        runner = self._ctx.runner_options(self.model).model_dump(exclude_none=True)
        return AgentLLMConfigOut(provider="ollama", model=self.model, base_url=self._ctx.ollama.host,
                                 options={**self.options.model_dump(), "runner": runner})
