"""Shared runtime handed to every provider: settings, one httpx client, one Ollama API wrapper."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from clicky.capabilities.ollama_api import OllamaAPI
from clicky.capabilities.profiles import DERIVED_MODELS, runner_options_for
from clicky.config import RunnerOptions, Settings


@dataclass(frozen=True)
class ProviderContext:
    settings: Settings
    http: httpx.AsyncClient          # owned (and closed) by the Capabilities registry, never by a provider
    ollama: OllamaAPI

    @classmethod
    def create(cls, settings: Settings, http: httpx.AsyncClient) -> ProviderContext:
        ollama = OllamaAPI(http, settings.ollama.host, request_timeout_s=settings.ollama.request_timeout_s)
        return cls(settings=settings, http=http, ollama=ollama)

    def runner_options(self, model: str) -> RunnerOptions:
        return runner_options_for(model, self.settings)

    def pull_hint(self, model: str) -> str:
        """How to make `model` available locally."""
        profile = self.settings.profile.value
        if model in DERIVED_MODELS:
            return f"run: clicky models pull --profile {profile}   (creates the CPU-pinned {model})"
        return f"run: ollama pull {model}   (or: clicky models pull --profile {profile})"


def new_http_client() -> httpx.AsyncClient:
    """The registry's shared client. trust_env=False: proxies from the environment must not intercept localhost."""
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0), trust_env=False)
