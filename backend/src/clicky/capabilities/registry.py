"""Capability registry: every capability resolved to a health-checked fallback chain.

`build_capabilities(settings)`:
1. tier: settings.hardware_tier, or `hardware.detect_tier()` when "auto" (only the local/hybrid profiles need one)
2. chain per capability: the `[profiles.<profile>.capabilities.<cap>]` override if present (it replaces the preset
   as a whole, fallbacks included), else the preset from profiles.py; the fake profile ignores overrides. A provider
   picked on the options page (ui_settings.py) goes first, with that chain behind it as its fallbacks
3. validation, which fails startup: provider names against the catalog, options against the adapter's options
   model, a model id where the adapter needs one
4. per chain member: required packages (importlib.util.find_spec, nothing imported), API key, lazy import, health()
5. active member = the first healthy one, else the first member (unhealthy, with reason + hint): an unhealthy
   provider never stops the server from starting

`Capabilities.reconfigure()` switches profile or providers at runtime: unchanged chains keep their providers
(and loaded models), changed ones are built and health-checked, replaced providers close after a grace period.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import ValidationError

from clicky.capabilities.base import Health, ModelInfo, Provider, ProviderSpec
from clicky.capabilities.catalog import get_spec
from clicky.capabilities.context import ProviderContext, new_http_client
from clicky.capabilities.kinds import Capability, HardwareTier, Profile
from clicky.capabilities.options import AdapterOptions, adapter_for
from clicky.capabilities.profiles import needs_tier, preset_label, presets_for
from clicky.config import ProviderChoice, Settings
from clicky.protocol.events import CapabilityStatus
from clicky.ui_settings import UiSettings, load_ui_settings, ui_choice

log = logging.getLogger(__name__)

HEALTH_TIMEOUT_S = 10.0
SPEECH_REWARM_S = 120.0     # push-to-talk re-warms speech models at most this often
RETIRE_GRACE_S = 600.0      # a provider replaced at runtime closes this long after the switch (turns may still use it)

SystemFor = Callable[[ModelInfo | None], str]     # the brain's system prompt for a model (brain/prompts)


class ConfigError(ValueError):
    """Invalid provider configuration (unknown provider, bad options, missing model). Fails startup."""


# ------------------------------------------------------------------------------------------------- resolution


@dataclass(frozen=True)
class ResolvedMember:
    spec: ProviderSpec
    model: str | None
    options: AdapterOptions

    @property
    def label(self) -> str:
        return f"{self.spec.name}:{self.model}" if self.model else self.spec.name


@dataclass(frozen=True)
class ResolvedChain:
    capability: Capability
    members: tuple[ResolvedMember, ...]
    source: str              # which preset or override supplied the chain


def resolve_tier(settings: Settings) -> HardwareTier:
    if settings.hardware_tier is not HardwareTier.AUTO or not needs_tier(settings.profile):
        return settings.hardware_tier
    from clicky.capabilities.hardware import detect_tier  # runs nvidia-smi; only needed for local/hybrid

    return detect_tier()


def effective_choices(settings: Settings, tier: HardwareTier, profile: Profile | None = None,
                      ui: UiSettings | None = None) -> dict[Capability, tuple[ProviderChoice, str]]:
    """The ProviderChoice (and its source) for every capability. Overrides replace presets atomically; the options
    page's pick (`ui`, read from CLICKY_HOME when not given) goes in front, with that chain as its fallbacks."""
    active = profile or settings.profile
    presets = presets_for(active, tier)
    overrides = settings.overrides_for(active)
    picks = ui if ui is not None else load_ui_settings(settings)
    label = preset_label(active, tier)
    result: dict[Capability, tuple[ProviderChoice, str]] = {}
    for cap in Capability:
        if cap in overrides:
            base, source = overrides[cap], f"override [profiles.{active.value}.capabilities.{cap.value}]"
        else:
            base, source = presets[cap], label
        picked = ui_choice(picks, active, cap)
        result[cap] = ((picked.model_copy(update={"fallbacks": [base]}), f"options page, then {source}")
                       if picked is not None else (base, source))
    return result


def _flatten(choice: ProviderChoice) -> Iterator[ProviderChoice]:
    yield choice
    for fallback in choice.fallbacks:
        yield from _flatten(fallback)


def _brief(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, err['loc'])) or 'options'}: {err['msg']}" for err in exc.errors())


def resolve_member(capability: Capability, choice: ProviderChoice, source: str) -> ResolvedMember:
    try:
        spec = get_spec(capability, choice.provider)
    except KeyError as exc:
        raise ConfigError(f"{source}: {exc.args[0]}") from None
    adapter = adapter_for(capability, choice.provider)
    model = choice.model or adapter.default_model
    if adapter.needs_model and not model:
        raise ConfigError(f"{source}: {capability.value}/{choice.provider} needs a `model`")
    try:
        options = adapter.options.model_validate(choice.options)
    except ValidationError as exc:
        raise ConfigError(f"{source}: invalid options for {capability.value}/{choice.provider}: {_brief(exc)}") \
            from None
    return ResolvedMember(spec, model, options)


def resolve_chain(capability: Capability, choice: ProviderChoice, source: str) -> ResolvedChain:
    members: list[ResolvedMember] = []
    seen: set[tuple[str, str | None]] = set()
    for item in _flatten(choice):
        member = resolve_member(capability, item, source)
        if (member.spec.name, member.model) not in seen:
            seen.add((member.spec.name, member.model))
            members.append(member)
    return ResolvedChain(capability, tuple(members), source)


def resolve_all(settings: Settings, tier: HardwareTier, profile: Profile | None = None,
                ui: UiSettings | None = None) -> dict[Capability, ResolvedChain]:
    """Validate the whole configuration without importing or contacting any provider."""
    return {cap: resolve_chain(cap, choice, source)
            for cap, (choice, source) in effective_choices(settings, tier, profile, ui).items()}


# --------------------------------------------------------------------------------------------- built chains


@dataclass
class ChainMember:
    resolved: ResolvedMember
    provider: Provider | None        # None when it could not even be instantiated (missing package / API key)
    health: Health

    @property
    def spec(self) -> ProviderSpec:
        return self.resolved.spec

    @property
    def model(self) -> str | None:
        return self.resolved.model

    @property
    def label(self) -> str:
        return self.resolved.label

    @property
    def usable(self) -> bool:
        return self.health.ok and self.provider is not None


@dataclass
class CapabilityChain:
    capability: Capability
    members: list[ChainMember]
    source: str
    active_index: int = 0

    @property
    def active(self) -> ChainMember:
        return self.members[self.active_index]

    def candidates(self) -> list[ChainMember]:
        """Runtime order: the active member, then every healthy member after it."""
        later = [m for m in self.members[self.active_index + 1:] if m.usable]
        return [self.active, *later]

    def status(self) -> CapabilityStatus:
        active = self.active
        others = [m.label for i, m in enumerate(self.members) if i != self.active_index]
        return CapabilityStatus(
            capability=self.capability.value, provider=active.spec.name, model=active.model,
            kind=active.spec.kind.value, source=active.spec.source.value, ok=active.health.ok,
            reason=active.health.reason, hint=active.health.hint, chain=[active.label, *others],
        )


def instantiate(member: ResolvedMember, context: ProviderContext) -> Provider:
    module_name, _, attr = member.spec.target.partition(":")
    cls = getattr(importlib.import_module(module_name), attr)
    return cls(member.spec, model=member.model, options=member.options, context=context)


def missing_modules(spec: ProviderSpec) -> list[str]:
    missing = []
    for name in spec.requires:
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(name)
    return missing


async def check_member(member: ResolvedMember, context: ProviderContext) -> ChainMember:
    spec = member.spec
    if missing := missing_modules(spec):
        hint = f"uv sync --extra {spec.extra}" if spec.extra else f"install {', '.join(missing)}"
        return ChainMember(member, None, Health(False, f"missing package: {', '.join(missing)}", hint))
    if spec.secret and not context.settings.secret_value(spec.secret):
        env_name = spec.secret.upper()
        return ChainMember(member, None, Health(False, f"{env_name} is not set",
                                                f"add {env_name}=... to {context.settings.home / '.env'}"))
    try:
        provider = instantiate(member, context)
    except Exception as exc:
        log.warning("cannot load %s: %s", spec.target, exc)
        return ChainMember(member, None, Health(False, f"cannot load {spec.target}: {exc}"))
    try:
        health = await asyncio.wait_for(provider.health(), HEALTH_TIMEOUT_S)
    except TimeoutError:
        health = Health(False, f"health check timed out after {HEALTH_TIMEOUT_S:.0f} s")
    except Exception as exc:
        health = Health(False, f"health check failed: {exc}")
    return ChainMember(member, provider, health)


BENCH_FILE = "bench.json"        # written by `clicky doctor --bench` (speech/bench.py)


def demoted_speech_providers(data_dir: Path) -> set[str]:
    """Labels of speech providers that missed the SLO in the last bench run (empty without a readable file)."""
    try:
        data = json.loads((data_dir / BENCH_FILE).read_text(encoding="utf-8"))
        return {r["provider"] for r in data.get("results", []) if not r.get("ok", True)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return set()


async def build_chain(resolved: ResolvedChain, context: ProviderContext) -> CapabilityChain:
    members = list(await asyncio.gather(*(check_member(m, context) for m in resolved.members)))
    active = next((i for i, m in enumerate(members) if m.usable), 0)
    if resolved.capability in (Capability.STT, Capability.TTS):
        demoted = demoted_speech_providers(context.settings.data_dir)
        better = next((i for i, m in enumerate(members) if m.usable and m.label not in demoted), None)
        if members[active].label in demoted and better is not None:
            log.info("%s: %s missed the speech SLO in the last bench; using %s", resolved.capability.value,
                     members[active].label, members[better].label)
            active = better
    return CapabilityChain(resolved.capability, members, resolved.source, active)


# --------------------------------------------------------------------------------------------- capabilities


class Capabilities:
    def __init__(self, *, settings: Settings, tier: HardwareTier, chains: Mapping[Capability, CapabilityChain],
                 context: ProviderContext, owns_http: bool, ui: UiSettings | None = None) -> None:
        self.settings = settings
        self.tier = tier
        self.chains = dict(chains)
        self.context = context
        self.ui = ui if ui is not None else UiSettings()
        self._owns_http = owns_http
        self._speech_warmed_at = -SPEECH_REWARM_S
        self._background: set[asyncio.Task[None]] = set()
        self._retired: list[Provider] = []

    @property
    def profile(self) -> Profile:
        return self.settings.profile

    def get(self, capability: Capability) -> CapabilityChain:
        return self.chains[capability]

    def provider(self, capability: Capability) -> Provider | None:
        return self.chains[capability].active.provider

    def describe(self) -> list[CapabilityStatus]:
        return [self.chains[cap].status() for cap in Capability if cap in self.chains]

    async def reconfigure(self, settings: Settings, ui: UiSettings) -> list[Capability]:
        """Switch to `settings` + `ui` (another profile or provider) without a restart. Raises ConfigError, and
        then nothing changes. Unchanged chains keep their providers; changed ones are built and health-checked (the
        others keep serving meanwhile); providers no longer used close after RETIRE_GRACE_S. Returns what changed."""
        if settings.hardware_tier is not HardwareTier.AUTO:
            tier = settings.hardware_tier
        elif self.tier is not HardwareTier.AUTO:
            tier = self.tier
        else:
            tier = await asyncio.to_thread(resolve_tier, settings)
        resolved = resolve_all(settings, tier, ui=ui)
        context = ProviderContext.create(settings, self.context.http)
        kept: dict[Capability, CapabilityChain] = {}
        changed: list[ResolvedChain] = []
        for cap, chain in resolved.items():
            old = self.chains.get(cap)
            if old is not None and tuple(m.resolved for m in old.members) == chain.members:
                old.source = chain.source
                kept[cap] = old
            else:
                changed.append(chain)
        built = await asyncio.gather(*(build_chain(chain, context) for chain in changed))
        chains = {**kept, **{chain.capability: chain for chain in built}}
        live = {id(m.provider) for c in chains.values() for m in c.members if m.provider is not None}
        retired = {id(m.provider): m.provider for c in self.chains.values() for m in c.members
                   if m.provider is not None and id(m.provider) not in live}
        self.settings, self.tier, self.context, self.ui, self.chains = settings, tier, context, ui, chains
        if retired:
            self._retired.extend(retired.values())
            task = asyncio.create_task(self._close_retired(list(retired.values())), name="retire-providers")
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        for chain in built:
            log.info("%s now uses %s (%s)", chain.capability.value, chain.active.label,
                     "ok" if chain.active.health.ok else chain.active.health.reason)
        return [chain.capability for chain in built]

    async def _close_retired(self, providers: list[Provider]) -> None:
        await asyncio.sleep(RETIRE_GRACE_S)
        for provider in providers:
            if provider in self._retired:
                self._retired.remove(provider)
                with contextlib.suppress(Exception):
                    await provider.aclose()

    def schedule_warm_up(self, system_for: SystemFor | None) -> None:
        """After a switch: load the (new) brain and speech models in the background, as at startup."""
        async def warm() -> None:
            await self.warm_up(system_for)
            self._speech_warmed_at = time.monotonic()
            await self.warm_up_speech()

        task = asyncio.create_task(warm(), name="warm-up-after-switch")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def warm_up_speech(self) -> None:
        """Load the active STT and TTS models (once per model: lfm3 serves both). Never raises."""
        seen: set[tuple[str, str | None]] = set()
        for capability in (Capability.STT, Capability.TTS):
            chain = self.chains.get(capability)
            if chain is None or not chain.active.usable:
                continue
            member = chain.active
            warm_up = getattr(member.provider, "warm_up", None)
            if warm_up is None or (member.spec.source, member.model) in seen:
                continue
            seen.add((member.spec.source, member.model))
            try:
                await warm_up()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("speech warm-up failed for %s: %s", member.label, exc)

    def prewarm_speech(self) -> None:
        """Fire-and-forget speech warm-up when push-to-talk starts: a reload overlaps the user's speaking."""
        now = time.monotonic()
        if now - self._speech_warmed_at < SPEECH_REWARM_S:
            return
        self._speech_warmed_at = now
        task = asyncio.create_task(self.warm_up_speech(), name="speech-warm-up")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _brain_system(self, system_for: SystemFor | None) -> tuple[ChainMember | None, str | None]:
        chain = self.chains.get(Capability.BRAIN)
        if chain is None or not chain.active.usable:
            return None, None
        member = chain.active
        info = getattr(member.provider, "info", None)
        system = system_for(info() if callable(info) else None) if system_for is not None else None
        return member, system

    async def warm_up(self, system_for: SystemFor | None = None) -> None:
        """Load the brain model (Ollama: into VRAM) so the first turn doesn't pay for it; with `system_for`, also
        pre-fill the brain's system prompt (Ollama KV cache / Claude prompt cache). Never raises."""
        member, system = self._brain_system(system_for)
        warm_up = getattr(member.provider, "warm_up", None) if member is not None else None
        if member is None or warm_up is None:
            return
        started = time.perf_counter()
        try:
            await warm_up(system=system)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("brain warm-up failed for %s: %s", member.label, exc)
        else:
            log.info("brain ready: %s (warm-up %.1f s)", member.label, time.perf_counter() - started)

    def prewarm_brain(self, system_for: SystemFor) -> None:
        """Fire-and-forget when push-to-talk starts, for providers that benefit (Claude: re-write an expired prompt
        cache and reopen the connection while the user speaks). The provider throttles it."""
        member, system = self._brain_system(system_for)
        prewarm = getattr(member.provider, "prewarm", None) if member is not None else None
        if prewarm is None or system is None:
            return
        task = asyncio.create_task(self._quietly(prewarm(system), "brain pre-warm"), name="brain-prewarm")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @staticmethod
    async def _quietly(work: Awaitable[object], what: str) -> None:
        try:
            await work
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("%s failed: %s", what, exc)

    async def aclose(self) -> None:
        for task in list(self._background):
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        seen: set[int] = set()
        members = [(m.provider, m.label) for chain in self.chains.values() for m in chain.members]
        members += [(provider, "retired provider") for provider in self._retired]
        self._retired.clear()
        for provider, label in members:
            if provider is None or id(provider) in seen:
                continue
            seen.add(id(provider))
            try:
                await provider.aclose()
            except Exception as exc:
                log.warning("closing %s failed: %s", label, exc)
        if self._owns_http:
            await self.context.http.aclose()


async def build_capabilities(settings: Settings, *, http: httpx.AsyncClient | None = None,
                             tier: HardwareTier | None = None, ui: UiSettings | None = None) -> Capabilities:
    """Resolve, validate (ConfigError fails startup) and health-check every capability chain. `ui`: the options
    page's choices (read from CLICKY_HOME when not given)."""
    picks = ui if ui is not None else load_ui_settings(settings)
    resolved_tier = tier if tier is not None else await asyncio.to_thread(resolve_tier, settings)
    resolved = resolve_all(settings, resolved_tier, ui=picks)
    owns_http = http is None
    client = http if http is not None else new_http_client()
    context = ProviderContext.create(settings, client)
    try:
        built = await asyncio.gather(*(build_chain(chain, context) for chain in resolved.values()))
    except BaseException:
        if owns_http:
            await client.aclose()
        raise
    chains = {chain.capability: chain for chain in built}
    for chain in chains.values():
        active = chain.active
        if not active.health.ok:
            log.warning("%s: no healthy provider (%s: %s)", chain.capability.value, active.label,
                        active.health.reason)
    return Capabilities(settings=settings, tier=resolved_tier, chains=chains, context=context, owns_http=owns_http,
                        ui=picks)
