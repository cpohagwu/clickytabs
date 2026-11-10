"""`clicky` command line: serve | doctor | chat | eval brain | config show | models list/pull | token show/rotate |
schema export."""

from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import os
import sys
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from clicky import __version__
from clicky.api.health import WS_PATH
from clicky.capabilities.kinds import HardwareTier, Profile

app = typer.Typer(help="ClickyTabs orchestrator.", no_args_is_help=True, add_completion=False)
config_app = typer.Typer(help="Inspect the effective configuration.", no_args_is_help=True)
models_app = typer.Typer(help="List and fetch the models a profile needs.", no_args_is_help=True)
token_app = typer.Typer(help="Pairing token for the extension.", no_args_is_help=True)
schema_app = typer.Typer(help="Wire-protocol JSON Schema.", no_args_is_help=True)
eval_app = typer.Typer(help="Measure model quality and latency on scripted inputs.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(eval_app, name="eval")
app.add_typer(models_app, name="models")
app.add_typer(token_app, name="token")
app.add_typer(schema_app, name="schema")

console = Console(highlight=False)
err = Console(stderr=True, highlight=False)


def _settings(**overrides: object):
    from clicky.config import Settings

    return Settings(**{k: v for k, v in overrides.items() if v is not None})


# ------------------------------------------------------------------------------------------------------- serve


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Bind address (default 127.0.0.1).")] = None,
    port: Annotated[int | None, typer.Option(help="Port (default 8765).")] = None,
    profile: Annotated[Profile | None, typer.Option(help="local | cloud | hybrid | fake")] = None,
    log_level: Annotated[str | None, typer.Option(help="DEBUG | INFO | WARNING")] = None,
    reload: Annotated[bool, typer.Option(help="Auto-reload on code changes (development).")] = False,
) -> None:
    """Run the orchestrator (WebSocket at /v1/ws)."""
    import uvicorn

    # uvicorn's factory mode calls create_app() without arguments, so CLI flags travel through the environment.
    for key, value in (("HOST", host), ("PORT", port), ("PROFILE", profile.value if profile else None),
                       ("LOG_LEVEL", log_level)):
        if value is not None:
            os.environ[f"CLICKY_{key}"] = str(value)
    settings = _settings()
    if settings.host not in ("127.0.0.1", "localhost", "::1"):
        err.print(f"[yellow]warning:[/] binding to {settings.host} exposes the orchestrator beyond this machine; "
                  "use TLS (wss://) and keep the pairing token secret.")
    console.print(f"ClickyTabs {__version__}  profile=[bold]{settings.profile.value}[/]  "
                  f"ws://{settings.host}:{settings.port}{WS_PATH}")
    console.print(f"Pairing token file: {settings.token_path}   (show it with: [bold]uv run clicky token show[/])")
    uvicorn.run("clicky.main:create_app", factory=True, host=settings.host, port=settings.port,
                ws_ping_interval=20, log_config=None, timeout_graceful_shutdown=5, reload=reload,
                log_level=settings.log_level.lower())


# ------------------------------------------------------------------------------------------------------ doctor


@app.command()
def doctor(
    profile: Annotated[Profile | None, typer.Option(help="Profile to plan models for.")] = None,
    bench: Annotated[bool, typer.Option("--bench", help="Also measure the speech providers while the brain decodes "
                                        "(saves .clicky/bench.json; slow providers get demoted).")] = False,
    no_load: Annotated[bool, typer.Option("--no-load", help="Bench without the concurrent brain load.")] = False,
) -> None:
    """Probe this machine (GPU, RAM, Ollama, Hugging Face cache, Chrome) and show the model plan for its tier."""
    asyncio.run(_doctor(profile))
    if bench:
        asyncio.run(_bench(under_load=not no_load))


async def _bench(*, under_load: bool) -> None:
    from clicky.capabilities.registry import build_capabilities
    from clicky.speech.bench import STT_MAX_MS, TTS_FIRST_AUDIO_MS, TTS_MAX_RTF, run_bench, save_results

    settings = _settings()
    console.print(f"\nSpeech bench ({'while the brain decodes' if under_load else 'idle'}): "
                  f"TTS first audio <= {TTS_FIRST_AUDIO_MS:.0f} ms and RTF <= {TTS_MAX_RTF}; "
                  f"STT <= {STT_MAX_MS:.0f} ms ...")
    capabilities = await build_capabilities(settings)
    try:
        await capabilities.warm_up()
        await capabilities.warm_up_speech()
        results = await run_bench(capabilities, under_load=under_load)
    finally:
        await capabilities.aclose()
    table = Table(title="Speech bench", title_justify="left")
    for column in ("capability", "provider", "SLO", "measured"):
        table.add_column(column)
    for result in results:
        table.add_row(result.capability, result.provider, "[green]met[/]" if result.ok else "[red]missed[/]",
                      result.detail)
    console.print(table)
    path = save_results(settings.data_dir, results, settings.profile.value, under_load)
    console.print(f"Saved {path}. Providers that missed the SLO are skipped at startup when a faster healthy "
                  "provider exists in the same chain.")


async def _doctor(profile: Profile | None) -> None:
    from clicky.capabilities.context import new_http_client
    from clicky.capabilities.hardware import collect_report
    from clicky.capabilities.models import ModelManager, plan_for
    from clicky.capabilities.ollama_api import OllamaAPI
    from clicky.capabilities.profiles import preset_label

    settings = _settings()
    async with new_http_client() as http:
        api = OllamaAPI(http, settings.ollama.host, request_timeout_s=settings.ollama.request_timeout_s)
        report = await collect_report(api, settings.home)
        tier = settings.hardware_tier if settings.hardware_tier is not HardwareTier.AUTO else report.tier
        active = profile or (settings.profile if settings.profile is not Profile.FAKE else Profile.LOCAL)
        manager = ModelManager(settings, api)
        statuses = await manager.status(plan_for(settings, active, tier, include_fallbacks=True))
        await manager.aclose()

    s = report.system
    table = Table(title=f"ClickyTabs doctor ({__version__})", show_header=False, title_justify="left")
    table.add_row("System", s.os)
    table.add_row("CPU", f"{s.cpu} ({s.cores_physical} cores / {s.cores_logical} threads)")
    table.add_row("RAM", f"{s.ram_total_gib:.1f} GiB ({s.ram_available_gib:.1f} GiB available)")
    if report.nvidia and report.nvidia.gpus:
        for gpu in report.nvidia.gpus:
            table.add_row("GPU", f"{gpu.name}  VRAM {gpu.memory_total_mib} MiB (used {gpu.memory_used_mib} MiB)")
    else:
        table.add_row("GPU", "no NVIDIA GPU detected (nvidia-smi unavailable)")
    table.add_row("Hardware tier", f"[bold]{tier.value}[/]" + ("" if tier is report.tier else " (from settings)"))
    for disk in report.disks:
        table.add_row(f"Disk ({disk.label})",
                      f"{disk.free_gib:.0f} GiB free of {disk.total_gib:.0f} GiB at {disk.path}")
    o = report.ollama
    table.add_row("Ollama", f"{o.version} at {o.host}" if o.reachable else
                  f"[red]unreachable[/] at {settings.ollama.host} ({o.error})")
    if o.reachable:
        table.add_row("Ollama models", ", ".join(m.name for m in o.models) or "(none)")
        for running in o.running:
            table.add_row("Ollama loaded", f"{running.name}: {running.processor}")
    if report.ollama_env:
        shown = {k: v for k, v in report.ollama_env.items() if k in (
            "OLLAMA_FLASH_ATTENTION", "OLLAMA_KV_CACHE_TYPE", "OLLAMA_CONTEXT_LENGTH", "OLLAMA_MAX_LOADED_MODELS",
            "OLLAMA_NUM_PARALLEL", "OLLAMA_KEEP_ALIVE", "OLLAMA_HOST")}
        table.add_row("Ollama server env", ", ".join(f"{k}={v}" for k, v in shown.items()) or "(defaults)")
    table.add_row("HF cache", f"{len(report.hf_cache)} repos in {report.hf_cache_dir}")
    table.add_row("ffmpeg", report.ffmpeg or "[yellow]not found[/]")
    table.add_row("Chrome", report.chrome or "not found")
    console.print(table)

    if report.chrome_on_nvidia:
        console.print("[yellow]Chrome is using the NVIDIA GPU[/] - set Chrome to Power saving (iGPU) in Windows "
                      "Settings > System > Display > Graphics to leave VRAM for Ollama.")
    if tier is HardwareTier.GPU_4GB and report.ollama_env is not None \
            and report.ollama_env.get("OLLAMA_FLASH_ATTENTION", "").lower() not in ("1", "true"):
        console.print("Tip: setx OLLAMA_FLASH_ATTENTION 1, then restart the Ollama tray app.")

    plan = Table(title=f"Model plan: {preset_label(active, tier)}", title_justify="left")
    for column in ("artifact", "used by", "status", "action"):
        plan.add_column(column)
    for st in statuses:
        mark = "[green]present[/]" if st.present else "[yellow]missing[/]"
        plan.add_row(st.artifact.name, ", ".join(st.artifact.used_by), mark,
                     "" if st.present else st.artifact.describe_action())
    console.print(plan)
    missing = [st for st in statuses if not st.present]
    if missing:
        console.print(f"{len(missing)} artifact(s) missing. Fetch the primary ones with: "
                      f"[bold]uv run clicky models pull --profile {active.value}[/] "
                      "(add --include-fallbacks for the whole chain).")


# -------------------------------------------------------------------------------------------------------- chat


@app.command()
def chat(
    text: Annotated[str, typer.Argument(help="What to ask.")],
    provider: Annotated[str | None, typer.Option(help="Brain provider (default: the profile's).")] = None,
    model: Annotated[str | None, typer.Option(help="Model for --provider.")] = None,
    image: Annotated[Path | None, typer.Option(exists=True, dir_okay=False, help="Attach an image.")] = None,
    system: Annotated[str | None, typer.Option(help="System prompt override.")] = None,
) -> None:
    """Stream one brain answer in-process and print time-to-first-token, tokens/s and the Ollama GPU split."""
    code = asyncio.run(_chat(text, provider, model, image, system))
    raise typer.Exit(code)


async def _chat(text: str, provider: str | None, model: str | None, image: Path | None, system: str | None) -> int:
    from clicky.brain.prompts import system_prompt
    from clicky.capabilities.base import ChatMessage, ChatRequest, Finish, ImagePart, TextDelta, TextPart
    from clicky.capabilities.context import ProviderContext, new_http_client
    from clicky.capabilities.hardware import parse_ollama_running
    from clicky.capabilities.kinds import Capability
    from clicky.capabilities.registry import check_member, effective_choices, resolve_member, resolve_tier
    from clicky.config import ProviderChoice

    settings = _settings()
    if provider:
        choice, source = ProviderChoice(provider=provider, model=model), "--provider"
    else:
        tier = await asyncio.to_thread(resolve_tier, settings)
        choice, source = effective_choices(settings, tier)[Capability.BRAIN]
        if model:
            choice = choice.model_copy(update={"model": model, "fallbacks": []})
    member = resolve_member(Capability.BRAIN, choice, source)
    parts: list[TextPart | ImagePart] = [TextPart(text)]
    if image is not None:
        data = await asyncio.to_thread(image.read_bytes)
        parts.insert(0, ImagePart(data, mimetypes.guess_type(image.name)[0] or "image/png"))
    async with new_http_client() as http:
        context = ProviderContext.create(settings, http)
        checked = await check_member(member, context)
        if checked.provider is None or not checked.health.ok:
            err.print(f"[red]{checked.label} unavailable:[/] {checked.health.reason}")
            if checked.health.hint:
                err.print(checked.health.hint)
            return 1
        brain = checked.provider
        request = ChatRequest(system=system or system_prompt(brain.info()),   # type: ignore[attr-defined]
                              messages=(ChatMessage("user", tuple(parts)),), max_tokens=1024)
        console.print(f"[dim]{checked.label} ({source})[/]")
        started = time.perf_counter()
        first: float | None = None
        finish: Finish | None = None
        try:
            async with contextlib.aclosing(brain.stream(request)) as events:   # type: ignore[attr-defined]
                async for event in events:
                    if isinstance(event, TextDelta) and event.text:
                        first = first or time.perf_counter()
                        console.print(event.text, end="", soft_wrap=True)
                    elif isinstance(event, Finish):
                        finish = event
        finally:
            await brain.aclose()
        ended = time.perf_counter()
        console.print()
        stats = [f"stop={finish.stop_reason if finish else '?'}"]
        if first is not None:
            stats.append(f"TTFT {first - started:.2f} s")
            output = finish.usage.output_tokens if finish and finish.usage else None
            if output and ended > first:
                stats.append(f"{output / (ended - first):.1f} tok/s")
        if member.spec.name == "ollama":
            with contextlib.suppress(Exception):
                for running in parse_ollama_running(await context.ollama.ps()):
                    stats.append(f"{running.name}: {running.processor}")
        console.print("[dim]" + " | ".join(stats) + "[/]")
        return 0 if finish is not None and finish.stop_reason != "error" else 1


# -------------------------------------------------------------------------------------------------------- eval


@eval_app.command("brain")
def eval_brain(
    pages: Annotated[Path | None, typer.Option(help="Captured pages (default: .clicky/eval/pages; create them with "
                                               "`npx playwright test eval-capture` in extension/).")] = None,
    provider: Annotated[str | None, typer.Option(help="Brain provider (default: the profile's).")] = None,
    model: Annotated[str | None, typer.Option(help="Model for --provider.")] = None,
    effort: Annotated[str | None, typer.Option(help="Claude effort: low | medium | high | xhigh | max.")] = None,
    budget: Annotated[str, typer.Option(help="Page budget: auto (API models full, local compact) | full | "
                                        "compact.")] = "auto",
    cases: Annotated[str | None, typer.Option(help="Comma-separated case ids (default: all).")] = None,
) -> None:
    """Score the brain on the scripted pages: pointing accuracy, directive validity and latency."""
    raise typer.Exit(asyncio.run(_eval_brain(pages, provider, model, effort, budget, cases)))


async def _eval_brain(pages: Path | None, provider: str | None, model: str | None, effort: str | None,
                      budget: str, only: str | None) -> int:
    import json
    from dataclasses import asdict
    from datetime import datetime

    from clicky.brain.evaluate import load_cases, load_page, run_case, summarize
    from clicky.brain.prompts import system_prompt
    from clicky.capabilities.context import ProviderContext, new_http_client
    from clicky.capabilities.kinds import Capability, Source
    from clicky.capabilities.registry import check_member, effective_choices, resolve_member, resolve_tier
    from clicky.config import ProviderChoice

    settings = _settings()
    pages_dir = pages or settings.data_dir / "eval" / "pages"
    if provider:
        options = {"effort": effort} if effort and provider == "anthropic" else {}
        choice, source = ProviderChoice(provider=provider, model=model, options=options), "--provider"
    else:
        tier = await asyncio.to_thread(resolve_tier, settings)
        choice, source = effective_choices(settings, tier)[Capability.BRAIN]
        choice = choice.model_copy(update={"fallbacks": [], **({"model": model} if model else {})})
        if effort and choice.provider == "anthropic":
            choice = choice.model_copy(update={"options": {**choice.options, "effort": effort}})
    member = resolve_member(Capability.BRAIN, choice, source)
    selected = load_cases(only=set(only.split(",")) if only else None)
    chosen = budget if budget != "auto" else ("full" if member.spec.source is Source.API else "compact")
    if not (pages_dir / chosen).is_dir():
        err.print(f"[red]no captured pages in {pages_dir / chosen}[/] - run the capture first "
                  "(see backend/evals/brain/README.md)")
        return 1

    async with new_http_client() as http:
        context = ProviderContext.create(settings, http)
        checked = await check_member(member, context)
        if checked.provider is None or not checked.health.ok:
            err.print(f"[red]{checked.label} unavailable:[/] {checked.health.reason}")
            return 1
        brain = checked.provider
        info = brain.info()                                                  # type: ignore[attr-defined]
        label = checked.label + (f" effort={choice.options['effort']}" if "effort" in choice.options else "")
        console.print(f"Brain eval: {label}  ({len(selected)} cases, {chosen} pages, prompt {info.prompt})")
        try:
            with contextlib.suppress(Exception):          # as the server does: model loaded, system prompt cached
                await brain.warm_up(system=system_prompt(info))             # type: ignore[attr-defined]
            results = []
            for case in selected:
                result = await run_case(brain, info, case, load_page(pages_dir, chosen, case.id))  # type: ignore[arg-type]
                results.append(result)
                verdict = "[green]hit[/]" if result.hit else "[red]miss[/]"
                validity = "" if result.valid else " [yellow]invalid tag[/]"
                console.print(f"  {case.id:<13} {verdict}{validity}  first sentence "
                              f"{result.first_sentence_ms or 0:>6.0f} ms  total {result.total_ms:>6.0f} ms  "
                              f"[dim]{(result.error or result.spoken)[:70]}[/]")
        finally:
            await brain.aclose()
    summary = summarize(results)
    console.print(f"hits {summary['hits']}/{summary['cases']}  valid {summary['valid']}/{summary['cases']}  "
                  f"said {summary['said']}  p50 TTFT {summary['p50_ttft_ms']} ms  p50 first sentence "
                  f"{summary['p50_first_sentence_ms']} ms  p50 total {summary['p50_total_ms']} ms  "
                  f"tokens in/cache read/cache write/out {summary['avg_input_tokens']}/"
                  f"{summary['avg_cache_read_tokens']}/{summary['avg_cache_write_tokens']}/"
                  f"{summary['avg_output_tokens']}")
    out_dir = settings.data_dir / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = "".join(c if c.isalnum() or c in "-." else "-" for c in label)
    path = out_dir / f"brain-{slug}-{stamp}.json"
    path.write_text(json.dumps({"model": label, "budget": chosen, "prompt": info.prompt, "summary": summary,
                                "results": [asdict(r) for r in results]}, indent=2), encoding="utf-8")
    console.print(f"[dim]saved {path}[/]")
    return 0


# ------------------------------------------------------------------------------------------------------ config


@config_app.command("show")
def config_show(
    health: Annotated[bool, typer.Option(help="Also health-check every chain member.")] = True,
) -> None:
    """Effective provider chain per capability, where it came from, and its health."""
    asyncio.run(_config_show(health))


async def _config_show(health: bool) -> None:
    from clicky.capabilities.registry import build_capabilities, effective_choices, resolve_all, resolve_tier

    settings = _settings()
    tier = await asyncio.to_thread(resolve_tier, settings)
    console.print(f"home={settings.home}  profile=[bold]{settings.profile.value}[/]  tier={tier.value}  "
                  f"ollama={settings.ollama.host}  allowed extension ids={settings.allowed_extension_ids or 'any'}")
    sources = {cap: source for cap, (_, source) in effective_choices(settings, tier).items()}
    table = Table(title_justify="left")
    for column in ("capability", "chain (active first)", "source", "health"):
        table.add_column(column)
    if health:
        capabilities = await build_capabilities(settings, tier=tier)
        try:
            for status in capabilities.describe():
                verdict = "[green]ok[/]" if status.ok else f"[red]{status.reason}[/]" + (
                    f"\n[dim]{status.hint}[/]" if status.hint else "")
                table.add_row(status.capability, " -> ".join(status.chain), sources.get(status.capability, ""),
                              verdict)
        finally:
            await capabilities.aclose()
    else:
        for cap, chain in resolve_all(settings, tier).items():
            table.add_row(cap.value, " -> ".join(m.label for m in chain.members), chain.source, "")
    console.print(table)


# ------------------------------------------------------------------------------------------------------ models


@models_app.command("list")
def models_list(
    profile: Annotated[Profile | None, typer.Option(help="Profile (default: the configured one).")] = None,
    include_fallbacks: Annotated[bool, typer.Option(help="Whole fallback chains, not just primaries.")] = True,
    only: Annotated[str, typer.Option(help="Only these providers, comma-separated (e.g. kokoro).")] = "",
) -> None:
    """Artifacts the profile needs and whether they are present."""
    asyncio.run(_models(profile, include_fallbacks, pull=False, dry_run=True, only=_providers(only)))


@models_app.command("pull")
def models_pull(
    profile: Annotated[Profile | None, typer.Option(help="Profile (default: the configured one).")] = None,
    include_fallbacks: Annotated[bool, typer.Option(help="Also fetch fallback chain members.")] = False,
    dry_run: Annotated[bool, typer.Option(help="Only print what would be fetched.")] = False,
    only: Annotated[str, typer.Option(help="Only these providers, comma-separated, from anywhere in the chains "
                                           "(e.g. kokoro).")] = "",
) -> None:
    """Fetch missing models: Ollama pulls, CPU-pinned derived models, Hugging Face snapshots, pinned files."""
    raise typer.Exit(asyncio.run(_models(profile, include_fallbacks, pull=True, dry_run=dry_run,
                                         only=_providers(only))))


def _providers(value: str) -> frozenset[str]:
    return frozenset(part.strip() for part in value.split(",") if part.strip())


async def _models(profile: Profile | None, include_fallbacks: bool, *, pull: bool, dry_run: bool,
                  only: frozenset[str] = frozenset()) -> int:
    from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn

    from clicky.capabilities.context import new_http_client
    from clicky.capabilities.models import ModelManager, PullProgress, plan_for
    from clicky.capabilities.ollama_api import OllamaAPI
    from clicky.capabilities.registry import resolve_tier

    settings = _settings()
    active = profile or settings.profile
    tier = await asyncio.to_thread(resolve_tier, settings.model_copy(update={"profile": active}))
    async with new_http_client() as http:
        api = OllamaAPI(http, settings.ollama.host, request_timeout_s=settings.ollama.request_timeout_s)
        manager = ModelManager(settings, api)
        try:
            statuses = await manager.status(plan_for(settings, active, tier, include_fallbacks=include_fallbacks,
                                                          only=only))
            table = Table(title=f"{active.value} profile, tier {tier.value}", title_justify="left")
            for column in ("artifact", "kind", "used by", "status", "action"):
                table.add_column(column)
            for st in statuses:
                table.add_row(st.artifact.name, st.artifact.kind.value, ", ".join(st.artifact.used_by),
                              "[green]present[/]" if st.present else "[yellow]missing[/]",
                              "" if st.present else st.artifact.describe_action())
            console.print(table)
            missing = [st.artifact for st in statuses if not st.present]
            if not pull or not missing:
                return 0
            if dry_run:
                console.print(f"--dry-run: would fetch {len(missing)} artifact(s).")
                return 0
            failures = 0
            with Progress(TextColumn("{task.description}"), BarColumn(), DownloadColumn(),
                          console=console, transient=False) as bar:
                for artifact in missing:
                    task = bar.add_task(artifact.name, total=None)

                    def update(p: PullProgress, task: int = task) -> None:
                        bar.update(task, total=p.total, completed=p.completed or 0,
                                   description=f"{p.artifact}: {p.status}")

                    try:
                        await manager.fetch(artifact, update)
                        bar.update(task, description=f"{artifact.name}: done")
                    except Exception as exc:
                        failures += 1
                        bar.update(task, description=f"{artifact.name}: [red]failed[/] ({exc})")
            return 1 if failures else 0
        finally:
            await manager.aclose()


# ------------------------------------------------------------------------------------------------------- token


@token_app.command("show")
def token_show() -> None:
    """Print the pairing token (creating it on first use). Paste it into the extension's onboarding page."""
    settings = _settings()
    console.print(settings.resolved_token(), soft_wrap=True)
    err.print(f"[dim]stored in {settings.token_path}; backend URL ws://{settings.host}:{settings.port}{WS_PATH}[/]")


@token_app.command("rotate")
def token_rotate() -> None:
    """Issue a new token. Connected extensions get 4401 and must be re-paired."""
    settings = _settings()
    if settings.auth_token is not None:
        err.print("[yellow]CLICKY_AUTH_TOKEN is set; it overrides the token file.[/]")
    console.print(settings.rotate_token(), soft_wrap=True)


# ------------------------------------------------------------------------------------------------------ schema


@schema_app.command("export")
def schema_export(
    check: Annotated[bool, typer.Option(help="Exit 1 if protocol.schema.json is stale (CI drift gate).")] = False,
) -> None:
    """Write backend/protocol.schema.json (then run `npm run gen:protocol` in extension/)."""
    from clicky.protocol import export

    raise typer.Exit(export.main(["--check"] if check else []))


def main() -> None:   # pragma: no cover - console entry point
    app()


if __name__ == "__main__":   # pragma: no cover
    sys.exit(app())
