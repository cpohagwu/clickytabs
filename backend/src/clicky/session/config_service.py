"""The options page's view of the backend: `config.get` / `config.set` over the authenticated socket.

config.get -> config.state: the profile (and whether the environment pins it), every capability's active provider
with its health and fallback chain, the providers it could switch to (installed? key set?), local Ollama models, and
the TTS voices.

config.set -> validated against the catalog and the adapter's options, applied with `Capabilities.reconfigure()`
(no restart; unchanged chains keep their loaded models), saved to ui-settings.json only once it applied, and the new
state is broadcast to every session (the page budget follows the brain). One change at a time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from clicky.brain.budget import page_budget
from clicky.brain.prompts import system_prompt
from clicky.capabilities.base import ProviderSpec
from clicky.capabilities.catalog import get_spec, providers_for
from clicky.capabilities.kinds import Capability, Kind, Profile, Source
from clicky.capabilities.options import adapter_for
from clicky.capabilities.registry import Capabilities, CapabilityChain, ConfigError, missing_modules
from clicky.protocol.events import (
    CapabilityConfig,
    ConfigSet,
    ConfigState,
    Event,
    ProviderOption,
    VoiceOption,
)
from clicky.ui_settings import UiSettings, profile_pinned, save_ui_settings

log = logging.getLogger(__name__)

USER_PROFILES = (Profile.LOCAL, Profile.CLOUD, Profile.HYBRID)
OLLAMA_LIST_TIMEOUT_S = 3.0
VOICES_TIMEOUT_S = 6.0
MAX_VOICES = 200
PINNED_MESSAGE = ("The profile is set by CLICKY_PROFILE (or `clicky serve --profile`); change it there, or start the "
                  "backend without it to choose here.")


class ConfigService:
    def __init__(self, capabilities: Capabilities, broadcast: Callable[[Event], None]) -> None:
        self.capabilities = capabilities
        self._broadcast = broadcast
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------------------------------------ state

    async def state(self, request_id: str | None = None, error: str | None = None) -> ConfigState:
        caps = self.capabilities
        profile = caps.settings.profile
        models = await self._ollama_models()
        entries = []
        for capability in Capability:
            chain = caps.chains.get(capability)
            if chain is None:
                continue
            voices, voice_key = await self._voices(chain) if capability is Capability.TTS else ([], None)
            entries.append(CapabilityConfig(
                capability=capability.value, status=chain.status(), choice=caps.ui.choice(profile, capability),
                providers=[self._option(capability, spec, models) for spec in providers_for(capability)
                           if spec.kind is not Kind.TEST or profile is Profile.FAKE],
                voices=voices, voice_key=voice_key))
        profiles = [p.value for p in USER_PROFILES] + (["fake"] if profile is Profile.FAKE else [])
        return ConfigState(request_id=request_id, profile=profile.value, profile_pinned=profile_pinned(),
                           profiles=profiles, tier=caps.tier.value, capabilities=entries,
                           page_budget=page_budget(caps), error=error)

    def _option(self, capability: Capability, spec: ProviderSpec, models: list[str]) -> ProviderOption:
        adapter = adapter_for(capability, spec.name)
        reason = ""
        if missing := missing_modules(spec):
            hint = f" (uv sync --extra {spec.extra})" if spec.extra else ""
            reason = f"missing package {', '.join(missing)}{hint}"
        elif spec.secret and not self.capabilities.settings.secret_value(spec.secret):
            reason = f"{spec.secret.upper()} is not set in .env"
        return ProviderOption(name=spec.name, kind=spec.kind.value, source=spec.source.value, available=not reason,
                              reason=reason, default_model=adapter.default_model, needs_model=adapter.needs_model,
                              models=models if spec.source is Source.OLLAMA else [])

    async def _ollama_models(self) -> list[str]:
        try:
            return sorted(await asyncio.wait_for(self.capabilities.context.ollama.model_names(),
                                                 OLLAMA_LIST_TIMEOUT_S))
        except Exception:
            return []                          # Ollama not running: the page offers a free-text model field

    async def _voices(self, chain: CapabilityChain) -> tuple[list[VoiceOption], str | None]:
        member = chain.active
        fields = adapter_for(chain.capability, member.spec.name).options.model_fields
        voice_key = next((key for key in ("voice_id", "voice") if key in fields), None)
        lister = getattr(member.provider, "voices", None)
        if voice_key is None or lister is None or not member.usable:
            return [], voice_key
        try:
            listed = await asyncio.wait_for(lister(), VOICES_TIMEOUT_S)
        except Exception as exc:
            log.info("could not list voices of %s: %s", member.label, exc)
            return [], voice_key
        return [VoiceOption(id=vid[:200], name=name[:200]) for vid, name in listed[:MAX_VOICES]], voice_key

    # ------------------------------------------------------------------------------------------------ changes

    async def apply(self, request: ConfigSet) -> ConfigState:
        async with self._lock:
            caps = self.capabilities
            settings = caps.settings
            ui = caps.ui.model_copy(deep=True)
            if request.profile is not None and request.profile != settings.profile.value:
                if profile_pinned():
                    return await self.state(request.request_id, PINNED_MESSAGE)
                ui.profile = Profile(request.profile)
                settings = settings.model_copy(update={"profile": ui.profile})
            if request.capability is not None:
                error = self._change_capability(ui, settings.profile, request)
                if error:
                    return await self.state(request.request_id, error)
            elif request.profile is None:
                return await self.state(request.request_id, "Nothing to change.")
            try:
                changed = await caps.reconfigure(settings, ui)
            except ConfigError as exc:
                return await self.state(request.request_id, str(exc))
            error = None
            try:
                save_ui_settings(settings, ui)
            except OSError as exc:
                error = f"Applied, but could not be saved for the next start: {exc}"
            if changed:
                caps.schedule_warm_up(system_prompt)
            state = await self.state(error=error)
            self._broadcast(state)                 # every session (the page budget may have changed)
            return state.model_copy(update={"request_id": request.request_id})

    @staticmethod
    def _change_capability(ui: UiSettings, profile: Profile, request: ConfigSet) -> str | None:
        capability = Capability(request.capability)
        picks = dict(ui.capabilities.get(profile, {}))
        if request.reset:
            picks.pop(capability, None)
        elif request.choice is None:
            return "Pick a provider, or reset to the default."
        else:
            try:
                spec = get_spec(capability, request.choice.provider)
            except KeyError as exc:
                return str(exc.args[0])
            if spec.kind is Kind.TEST and profile is not Profile.FAKE:
                return "Test providers only run on the fake profile."
            picks[capability] = request.choice
        if picks:
            ui.capabilities[profile] = picks
        else:
            ui.capabilities.pop(profile, None)
        return None
