"""Hardware and environment probes for `clicky doctor` and hardware-tier detection.

Parsers are pure functions, unit-tested against captured tool output. Probes shell out with short timeouts and
return None when a tool is missing; nothing here is required for the server to run.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import psutil

from clicky.capabilities.kinds import HardwareTier
from clicky.capabilities.ollama_api import OllamaAPI, OllamaError

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
GIB = 1024**3

# ---------------------------------------------------------------------------------------------------- tools


def run_tool(args: list[str], *, timeout: float = 5.0) -> str | None:
    """stdout of a CLI tool, or None if it is missing, fails or times out."""
    exe = shutil.which(args[0])
    if exe is None:
        return None
    try:
        proc = subprocess.run([exe, *args[1:]], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, creationflags=_NO_WINDOW, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _int_or_none(text: str) -> int | None:
    match = re.search(r"\d+", text)
    return int(match.group()) if match and "N/A" not in text else None


# ---------------------------------------------------------------------------------------------------- NVIDIA


@dataclass(frozen=True)
class GpuInfo:
    name: str
    memory_total_mib: int
    memory_used_mib: int | None = None

    @property
    def memory_total_gib(self) -> float:
        return self.memory_total_mib / 1024


@dataclass(frozen=True)
class GpuProcess:
    pid: int
    kind: str                    # C (compute), G (graphics), C+G, M, M+C
    name: str                    # possibly truncated by nvidia-smi ("...ication\\chrome.exe")
    used_mib: int | None

    @property
    def basename(self) -> str:
        return re.split(r"[\\/]", self.name)[-1].lower()


@dataclass(frozen=True)
class NvidiaProbe:
    gpus: tuple[GpuInfo, ...]
    processes: tuple[GpuProcess, ...]

    @property
    def max_vram_gib(self) -> float | None:
        return max((g.memory_total_gib for g in self.gpus), default=None)


def parse_nvidia_smi_gpus(text: str) -> list[GpuInfo]:
    """Parse `nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader,nounits`."""
    gpus = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.strip().rsplit(",", 2)]
        if len(parts) != 3 or not parts[0]:
            continue
        total = _int_or_none(parts[1])
        if total is not None:
            gpus.append(GpuInfo(parts[0], total, _int_or_none(parts[2])))
    return gpus


_PROCESS_ROW = re.compile(
    r"^\|\s*(?P<gpu>\d+)\s+(?:(?P<gi>\S+)\s+(?P<ci>\S+)\s+)?(?P<pid>\d+)\s+(?P<kind>C\+G|M\+C|C|G|M)\s+"
    r"(?P<name>.+?)\s+(?P<mem>\d+\s*MiB|N/A)\s*\|$"
)


def parse_nvidia_smi_processes(text: str) -> list[GpuProcess]:
    """Parse the Processes table of plain `nvidia-smi` (the only view that also lists graphics processes)."""
    processes = []
    in_table = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if "Processes:" in line:
            in_table = True
            continue
        if in_table and (match := _PROCESS_ROW.match(line)):
            processes.append(GpuProcess(int(match["pid"]), match["kind"], match["name"].strip(),
                                        _int_or_none(match["mem"])))
    return processes


def probe_nvidia() -> NvidiaProbe | None:
    query = run_tool(["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"])
    if query is None:
        return None
    table = run_tool(["nvidia-smi"]) or ""
    return NvidiaProbe(tuple(parse_nvidia_smi_gpus(query)), tuple(parse_nvidia_smi_processes(table)))


def chrome_gpu_processes(processes: tuple[GpuProcess, ...] | list[GpuProcess]) -> list[GpuProcess]:
    return [p for p in processes if p.basename in ("chrome.exe", "chrome")]


def tier_for_vram(vram_gib: float | None) -> HardwareTier:
    if vram_gib is None or vram_gib < 3:
        return HardwareTier.CPU
    if vram_gib < 6:
        return HardwareTier.GPU_4GB
    if vram_gib < 16:
        return HardwareTier.GPU_8GB
    return HardwareTier.GPU_24GB


@cache
def detect_tier() -> HardwareTier:
    """Tier from the largest NVIDIA GPU (cpu when there is none). Cached: hardware doesn't change at runtime."""
    probe = probe_nvidia()
    return tier_for_vram(probe.max_vram_gib if probe else None)


# ----------------------------------------------------------------------------------------- CPU / RAM / disk


@dataclass(frozen=True)
class SystemInfo:
    os: str
    cpu: str
    cores_physical: int | None
    cores_logical: int | None
    ram_total_gib: float
    ram_available_gib: float


def _cpu_name() -> str:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    elif sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine()


def probe_system() -> SystemInfo:
    memory = psutil.virtual_memory()
    return SystemInfo(os=platform.platform(terse=True), cpu=_cpu_name(),
                      cores_physical=psutil.cpu_count(logical=False), cores_logical=psutil.cpu_count(),
                      ram_total_gib=memory.total / GIB, ram_available_gib=memory.available / GIB)


@dataclass(frozen=True)
class DiskInfo:
    label: str
    path: str
    total_gib: float
    free_gib: float


def probe_disk(label: str, path: Path) -> DiskInfo | None:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return None
    return DiskInfo(label, str(path), usage.total / GIB, usage.free / GIB)


# ---------------------------------------------------------------------------------------------------- Ollama


@dataclass(frozen=True)
class OllamaModel:
    name: str
    size_bytes: int | None
    capabilities: tuple[str, ...]
    parameter_size: str | None
    quantization: str | None


@dataclass(frozen=True)
class OllamaRunning:
    name: str
    size_bytes: int
    size_vram_bytes: int
    context_length: int | None

    @property
    def processor(self) -> str:
        return processor_split(self.size_bytes, self.size_vram_bytes)


@dataclass(frozen=True)
class OllamaProbe:
    host: str
    version: str | None
    models: tuple[OllamaModel, ...]
    running: tuple[OllamaRunning, ...]
    error: str | None = None

    @property
    def reachable(self) -> bool:
        return self.error is None


def processor_split(size: int, size_vram: int) -> str:
    """Same wording as `ollama ps`: "100% GPU", "100% CPU" or "54%/46% CPU/GPU"."""
    if size <= 0:
        return "unknown"
    gpu = round(100 * size_vram / size)
    if gpu >= 100:
        return "100% GPU"
    if gpu <= 0:
        return "100% CPU"
    return f"{100 - gpu}%/{gpu}% CPU/GPU"


def parse_ollama_running(entries: list[dict]) -> list[OllamaRunning]:
    return [OllamaRunning(str(e.get("name") or e.get("model")), int(e.get("size") or 0),
                          int(e.get("size_vram") or 0), e.get("context_length")) for e in entries]


def parse_ollama_tags(entries: list[dict]) -> list[OllamaModel]:
    models = []
    for entry in entries:
        details = entry.get("details") or {}
        models.append(OllamaModel(str(entry.get("name") or entry.get("model")), entry.get("size"),
                                  tuple(entry.get("capabilities") or ()), details.get("parameter_size"),
                                  details.get("quantization_level")))
    return models


async def probe_ollama(api: OllamaAPI) -> OllamaProbe:
    try:
        version = await api.version()
        tags = await api.tags(fresh=True)
        running = await api.ps()
    except OllamaError as exc:
        return OllamaProbe(api.host, None, (), (), error=str(exc))
    return OllamaProbe(api.host, version, tuple(parse_ollama_tags(tags)), tuple(parse_ollama_running(running)))


_SERVER_CONFIG = re.compile(r'msg="server config" env="map\[(?P<body>.*)\]"\s*$')
_ENV_KEY = re.compile(r"(?:^|\s)([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+):")


def parse_ollama_server_config(log_text: str) -> dict[str, str] | None:
    """Effective OLLAMA_* (and GPU) settings from the most recent `msg="server config"` line of server.log."""
    body = None
    for line in log_text.splitlines():
        if 'msg="server config"' in line and (match := _SERVER_CONFIG.search(line)):
            body = match["body"]
    if body is None:
        return None
    body = re.sub(r"\\(.)", r"\1", body)          # slog escapes backslashes and quotes inside quoted values
    keys = list(_ENV_KEY.finditer(body))
    return {
        key.group(1): body[key.end(): keys[i + 1].start() if i + 1 < len(keys) else len(body)].strip()
        for i, key in enumerate(keys)
    }


def ollama_server_log() -> Path | None:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        path = Path(base) / "Ollama" / "server.log" if base else None
    else:
        path = Path.home() / ".ollama" / "logs" / "server.log"
    return path if path is not None and path.is_file() else None


def read_ollama_server_env(path: Path | None = None, *, max_bytes: int = 1 << 20) -> dict[str, str] | None:
    """Best effort: server.log restarts with every server start, so its config line is near the top."""
    log_path = path or ollama_server_log()
    if log_path is None:
        return None
    try:
        with log_path.open("rb") as handle:
            head = handle.read(max_bytes)
    except OSError:
        return None
    return parse_ollama_server_config(head.decode("utf-8", errors="replace"))


def ollama_models_dir(server_env: dict[str, str] | None = None) -> Path:
    configured = os.environ.get("OLLAMA_MODELS") or (server_env or {}).get("OLLAMA_MODELS")
    return Path(configured) if configured else Path.home() / ".ollama" / "models"


# ------------------------------------------------------------------------------------------- Hugging Face cache


@dataclass(frozen=True)
class HFCacheEntry:
    repo_id: str
    size_bytes: int
    snapshots: int


def hf_cache_dir() -> Path:
    if value := os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE"):
        return Path(value)
    if value := os.environ.get("HF_HOME"):
        return Path(value) / "hub"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "huggingface" / "hub"


def _tree_size(root: Path) -> int:
    """Bytes under `root`, not following symlinks (snapshots link into blobs on POSIX)."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                info = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if not stat.S_ISLNK(info.st_mode):
                total += info.st_size
    return total


def _repo_dir(repo_id: str, cache_dir: Path | None = None) -> Path:
    return (cache_dir or hf_cache_dir()) / f"models--{repo_id.replace('/', '--')}"


def hf_snapshot_count(repo_id: str, cache_dir: Path | None = None) -> int:
    snapshots = _repo_dir(repo_id, cache_dir) / "snapshots"
    if not snapshots.is_dir():
        return 0
    return sum(1 for snap in snapshots.iterdir() if snap.is_dir() and any(snap.iterdir()))


def list_hf_cache(cache_dir: Path | None = None) -> list[HFCacheEntry]:
    root = cache_dir or hf_cache_dir()
    if not root.is_dir():
        return []
    entries = []
    for repo in sorted(root.iterdir()):
        if repo.is_dir() and repo.name.startswith("models--"):
            repo_id = repo.name.removeprefix("models--").replace("--", "/", 1)
            entries.append(HFCacheEntry(repo_id, _tree_size(repo), hf_snapshot_count(repo_id, root)))
    return entries


# ------------------------------------------------------------------------------------------- ffmpeg / Chrome


def parse_ffmpeg_version(text: str) -> str | None:
    match = re.search(r"ffmpeg version (\S+)", text)
    return match.group(1) if match else None


def probe_ffmpeg() -> str | None:
    output = run_tool(["ffmpeg", "-version"])
    return parse_ffmpeg_version(output) if output else None


def probe_chrome() -> str | None:
    """Chrome's version. Windows: HKCU\\Software\\Google\\Chrome\\BLBeacon `version`; elsewhere `--version`."""
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon") as key:
                return str(winreg.QueryValueEx(key, "version")[0])
        except OSError:
            return None
    for exe in ("google-chrome", "google-chrome-stable", "chromium",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        output = run_tool([exe, "--version"])
        if output and (match := re.search(r"\d+(?:\.\d+){1,3}", output)):
            return match.group()
    return None


# ---------------------------------------------------------------------------------------------------- report


@dataclass(frozen=True)
class DoctorReport:
    system: SystemInfo
    nvidia: NvidiaProbe | None
    tier: HardwareTier
    disks: tuple[DiskInfo, ...]
    ollama: OllamaProbe
    ollama_env: dict[str, str] | None
    ollama_log: Path | None
    hf_cache_dir: Path
    hf_cache: tuple[HFCacheEntry, ...]
    ffmpeg: str | None
    chrome: str | None

    @property
    def chrome_on_nvidia(self) -> list[GpuProcess]:
        return chrome_gpu_processes(self.nvidia.processes) if self.nvidia else []


async def collect_report(api: OllamaAPI, home: Path) -> DoctorReport:
    nvidia, system, server_env, hf_entries, ffmpeg, chrome = await asyncio.gather(
        asyncio.to_thread(probe_nvidia),
        asyncio.to_thread(probe_system),
        asyncio.to_thread(read_ollama_server_env),
        asyncio.to_thread(list_hf_cache),
        asyncio.to_thread(probe_ffmpeg),
        asyncio.to_thread(probe_chrome),
    )
    ollama = await probe_ollama(api)
    disks = tuple(d for d in (probe_disk("CLICKY_HOME", home),
                              probe_disk("Ollama models", ollama_models_dir(server_env))) if d is not None)
    return DoctorReport(
        system=system, nvidia=nvidia, tier=tier_for_vram(nvidia.max_vram_gib if nvidia else None), disks=disks,
        ollama=ollama, ollama_env=server_env, ollama_log=ollama_server_log(), hf_cache_dir=hf_cache_dir(),
        hf_cache=tuple(hf_entries), ffmpeg=ffmpeg, chrome=chrome,
    )
