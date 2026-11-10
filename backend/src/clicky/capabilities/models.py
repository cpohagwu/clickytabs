"""ModelManager: which local artifacts a profile needs, whether they are present, and how to fetch them.

Artifact kinds:
- `ollama`          POST /api/pull (NDJSON progress)
- `ollama-derived`  POST /api/create {"model", "from", "parameters"}: CPU-pinned copies (num_gpu 0, num_thread 4)
- `huggingface`     huggingface_hub.snapshot_download with allow_patterns (`local` extra, imported lazily)
- `url`             plain files (Kokoro's ONNX model ships as GitHub release assets) into CLICKY_HOME/.clicky/models

By default a plan covers the primary provider of every capability except the optional embeddings; fallbacks and
optional capabilities are opt-in, so `clicky models pull` never downloads a whole chain unasked.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

from clicky.capabilities.hardware import hf_snapshot_count
from clicky.capabilities.kinds import Capability, HardwareTier, Profile
from clicky.capabilities.ollama_api import OllamaAPI, OllamaError, model_tag
from clicky.capabilities.options import ParakeetOptions
from clicky.capabilities.profiles import DERIVED_MODELS
from clicky.capabilities.registry import ResolvedMember, resolve_all
from clicky.config import Settings


class ArtifactKind(StrEnum):
    OLLAMA = "ollama"
    OLLAMA_DERIVED = "ollama-derived"
    HUGGINGFACE = "huggingface"
    URL = "url"


# faster-whisper size aliases -> Hugging Face repos (faster_whisper/utils.py `_MODELS`)
FASTER_WHISPER_REPOS = {
    "tiny.en": "Systran/faster-whisper-tiny.en", "base.en": "Systran/faster-whisper-base.en",
    "small.en": "Systran/faster-whisper-small.en", "medium.en": "Systran/faster-whisper-medium.en",
    "large-v3": "Systran/faster-whisper-large-v3", "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
    "distil-large-v3.5": "distil-whisper/distil-large-v3.5-ct2",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}
FASTER_WHISPER_FILES = ("config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*")
# onnx-asr model names -> Hugging Face repos (onnx_asr/resolver.py)
PARAKEET_REPOS = {"nemo-parakeet-tdt-0.6b-v2": "istupakov/parakeet-tdt-0.6b-v2-onnx"}
SILERO_VAD_REPO = "istupakov/silero-vad-onnx"
KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"
# name -> sha256 (the release's published asset digests; 310 MB + 27 MB)
KOKORO_FILES = {
    "kokoro-v1.0.onnx": "beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}


@dataclass(frozen=True)
class Artifact:
    kind: ArtifactKind
    name: str                                     # Ollama tag | HF repo id | path under .clicky/models (url)
    used_by: tuple[str, ...] = ()
    base: str | None = None                       # ollama-derived: FROM model
    parameters: tuple[tuple[str, int], ...] = ()  # ollama-derived: baked-in PARAMETERs
    allow_patterns: tuple[str, ...] = ()          # huggingface
    url: str | None = None                        # url
    sha256: str | None = None                     # url

    @property
    def key(self) -> tuple[ArtifactKind, str]:
        return self.kind, self.name

    def describe_action(self) -> str:
        """The exact action `clicky models pull` performs, as a copy-pasteable command where one exists."""
        if self.kind is ArtifactKind.OLLAMA:
            return f"ollama pull {self.name}"
        if self.kind is ArtifactKind.OLLAMA_DERIVED:
            params = ", ".join(f"{k} {v}" for k, v in self.parameters)
            return f"ollama create {self.name}   # Modelfile: FROM {self.base} + PARAMETER {params}"
        if self.kind is ArtifactKind.HUGGINGFACE:
            includes = "".join(f" --include '{p}'" for p in self.allow_patterns)
            return f"hf download {self.name}{includes}"
        return f"download {self.url}"


@dataclass(frozen=True)
class ArtifactStatus:
    artifact: Artifact
    present: bool
    detail: str = ""


@dataclass(frozen=True)
class PullProgress:
    artifact: str
    status: str
    digest: str | None = None
    completed: int | None = None
    total: int | None = None


ProgressCallback = Callable[[PullProgress], None]


class MissingExtra(RuntimeError):
    def __init__(self, package: str, extra: str) -> None:
        super().__init__(f"{package} is not installed: run `uv sync --extra {extra}`")
        self.package = package
        self.extra = extra


def parse_pull_line(obj: dict[str, Any], artifact: str) -> PullProgress:
    """One NDJSON object from /api/pull or /api/create. An `error` object raises OllamaError."""
    if obj.get("error"):
        raise OllamaError(str(obj["error"]))
    completed, total = obj.get("completed"), obj.get("total")
    return PullProgress(artifact, str(obj.get("status", "")), obj.get("digest"),
                        int(completed) if completed is not None else None,
                        int(total) if total is not None else None)


# ------------------------------------------------------------------------------------------------- planning


def artifacts_for(member: ResolvedMember) -> list[Artifact]:
    """Local artifacts one chain member needs (none for paid APIs and fakes)."""
    name, model = member.spec.name, member.model or ""
    if name in ("ollama", "ollama_audio"):
        derived = DERIVED_MODELS.get(model)
        if derived is None:
            return [Artifact(ArtifactKind.OLLAMA, model)]
        return [Artifact(ArtifactKind.OLLAMA, derived.base),
                Artifact(ArtifactKind.OLLAMA_DERIVED, derived.name, base=derived.base,
                         parameters=derived.parameters)]
    if name == "faster_whisper":
        return [Artifact(ArtifactKind.HUGGINGFACE, FASTER_WHISPER_REPOS.get(model, model),
                         allow_patterns=FASTER_WHISPER_FILES)]
    if name == "parakeet":
        options = member.options
        quantized = isinstance(options, ParakeetOptions) and options.quantization == "int8"
        weights = ("*-model.int8.onnx",) if quantized else ("*-model.onnx", "*-model.onnx?data")
        return [Artifact(ArtifactKind.HUGGINGFACE, PARAKEET_REPOS.get(model, model),
                         allow_patterns=("config.json", "config.yaml", "vocab.txt", *weights)),
                Artifact(ArtifactKind.HUGGINGFACE, SILERO_VAD_REPO, allow_patterns=("*.onnx", "*.json", "*.yaml"))]
    if name == "kokoro":
        return [Artifact(ArtifactKind.URL, f"kokoro/{file}", url=f"{KOKORO_RELEASE}/{file}", sha256=digest)
                for file, digest in KOKORO_FILES.items()]
    return []


def plan_for(settings: Settings, profile: Profile, tier: HardwareTier, *, include_fallbacks: bool = False,
             include_optional: bool = False, only: frozenset[str] = frozenset()) -> list[Artifact]:
    """Deduplicated artifacts for `profile` at `tier` (overrides from clicky.toml included), bases before derived.

    `only` keeps the members of those providers (e.g. {"kokoro"}), looking through whole chains."""
    merged: dict[tuple[ArtifactKind, str], Artifact] = {}
    for capability, chain in resolve_all(settings, tier, profile).items():
        if capability is Capability.EMBEDDINGS and not include_optional and not only:
            continue
        members = chain.members if include_fallbacks or only else chain.members[:1]
        for member in members:
            if only and member.spec.name not in only:
                continue
            for artifact in artifacts_for(member):
                known = merged.get(artifact.key, artifact)
                if capability.value not in known.used_by:
                    known = replace(known, used_by=(*known.used_by, capability.value))
                merged[artifact.key] = known
    return list(merged.values())


# -------------------------------------------------------------------------------------------------- manager


@dataclass
class ModelManager:
    settings: Settings
    ollama: OllamaAPI
    downloads: httpx.AsyncClient | None = None    # for url artifacts (honours proxy env vars); created lazily
    _owns_downloads: bool = field(default=False, init=False)

    @property
    def models_dir(self) -> Path:
        return self.settings.data_dir / "models"

    def url_path(self, artifact: Artifact) -> Path:
        return self.models_dir / artifact.name

    async def status(self, artifacts: Iterable[Artifact]) -> list[ArtifactStatus]:
        items = list(artifacts)
        ollama_names: set[str] | None = None
        ollama_error = ""
        if any(a.kind in (ArtifactKind.OLLAMA, ArtifactKind.OLLAMA_DERIVED) for a in items):
            try:
                ollama_names = await self.ollama.model_names(fresh=True)
            except OllamaError as exc:
                ollama_error = str(exc)
        result = []
        for artifact in items:
            if artifact.kind in (ArtifactKind.OLLAMA, ArtifactKind.OLLAMA_DERIVED):
                if ollama_names is None:
                    result.append(ArtifactStatus(artifact, False, ollama_error))
                else:
                    result.append(ArtifactStatus(artifact, model_tag(artifact.name) in ollama_names))
            elif artifact.kind is ArtifactKind.HUGGINGFACE:
                count = await asyncio.to_thread(hf_snapshot_count, artifact.name)
                result.append(ArtifactStatus(artifact, count > 0, "in the Hugging Face cache" if count else ""))
            else:
                path = self.url_path(artifact)
                result.append(ArtifactStatus(artifact, path.is_file(), str(path)))
        return result

    async def _stream_ollama(self, path: str, body: dict[str, Any], artifact: str,
                             on_progress: ProgressCallback | None) -> None:
        last = ""
        async with contextlib.aclosing(self.ollama.stream(path, body)) as lines:
            async for obj in lines:
                progress = parse_pull_line(obj, artifact)
                last = progress.status
                if on_progress is not None:
                    on_progress(progress)
        self.ollama.invalidate()
        if last != "success":
            raise OllamaError(f"{path} for {artifact} ended with status {last!r}")

    async def pull_ollama(self, model: str, on_progress: ProgressCallback | None = None) -> None:
        await self._stream_ollama("/api/pull", {"model": model}, model, on_progress)

    async def create_derived(self, artifact: Artifact, on_progress: ProgressCallback | None = None) -> None:
        body = {"model": artifact.name, "from": artifact.base, "parameters": dict(artifact.parameters)}
        await self._stream_ollama("/api/create", body, artifact.name, on_progress)

    async def download_hf(self, artifact: Artifact) -> Path:
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise MissingExtra("huggingface_hub", "local") from exc
        path = await asyncio.to_thread(snapshot_download, repo_id=artifact.name,
                                       allow_patterns=list(artifact.allow_patterns) or None)
        return Path(path)

    async def download_url(self, artifact: Artifact, on_progress: ProgressCallback | None = None) -> Path:
        if artifact.url is None:
            raise ValueError(f"{artifact.name} has no url")
        if self.downloads is None:
            self.downloads = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60.0, connect=10.0))
            self._owns_downloads = True
        dest = self.url_path(artifact)
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".part")
        digest = hashlib.sha256()
        async with self.downloads.stream("GET", artifact.url) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0) or None
            done = 0
            with partial.open("wb") as handle:
                async for chunk in response.aiter_bytes(1 << 20):
                    handle.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if on_progress is not None:
                        on_progress(PullProgress(artifact.name, "downloading", completed=done, total=total))
        if artifact.sha256 and digest.hexdigest() != artifact.sha256:
            partial.unlink(missing_ok=True)
            raise ValueError(f"sha256 mismatch for {artifact.name}")
        os.replace(partial, dest)
        return dest

    async def fetch(self, artifact: Artifact, on_progress: ProgressCallback | None = None) -> None:
        if artifact.kind is ArtifactKind.OLLAMA:
            await self.pull_ollama(artifact.name, on_progress)
        elif artifact.kind is ArtifactKind.OLLAMA_DERIVED:
            await self.create_derived(artifact, on_progress)
        elif artifact.kind is ArtifactKind.HUGGINGFACE:
            await self.download_hf(artifact)
        else:
            await self.download_url(artifact, on_progress)

    async def aclose(self) -> None:
        if self._owns_downloads and self.downloads is not None:
            await self.downloads.aclose()
