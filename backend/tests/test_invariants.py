"""Catalog invariants (the model-agnostic promise), redaction, hardware parsers and the protocol drift gate."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging

import pytest

from clicky.capabilities._stub import StubProvider
from clicky.capabilities.base import CAPABILITY_PROTOCOLS
from clicky.capabilities.catalog import CATALOG, providers_for
from clicky.capabilities.hardware import (
    parse_nvidia_smi_gpus,
    parse_nvidia_smi_processes,
    parse_ollama_server_config,
    processor_split,
    tier_for_vram,
)
from clicky.capabilities.kinds import Capability, HardwareTier, Kind
from clicky.capabilities.options import ADAPTERS
from clicky.logging import RedactingFilter
from clicky.protocol import export

# --------------------------------------------------------------------------------------------------- catalog


@pytest.mark.parametrize("capability", list(Capability))
def test_every_capability_has_paid_and_open_providers(capability: Capability) -> None:
    kinds = {spec.kind for spec in providers_for(capability)}
    assert Kind.PAID in kinds and Kind.OPEN in kinds


def test_every_catalog_row_has_an_options_model() -> None:
    for spec in CATALOG:
        assert (spec.capability, spec.name) in ADAPTERS, spec.key


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.key)
def test_catalog_rows_import_and_conform(spec) -> None:
    missing = [m for m in spec.requires if importlib.util.find_spec(m) is None]
    module_name, _, attr = spec.target.partition(":")
    cls = getattr(importlib.import_module(module_name), attr)   # targets must import without their extras
    protocol = CAPABILITY_PROTOCOLS[spec.capability]
    required = {name for name, value in vars(protocol).items()
                if callable(value) and not name.startswith("_")} | {"health", "aclose"}
    for name in required:
        assert callable(getattr(cls, name, None)), f"{spec.key}: missing {name}()"
    if missing:
        pytest.skip(f"{spec.key}: extra not installed ({', '.join(missing)})")


def _is_stub(spec) -> bool:
    module_name, _, attr = spec.target.partition(":")
    return issubclass(getattr(importlib.import_module(module_name), attr), StubProvider)


@pytest.mark.parametrize("spec", [s for s in CATALOG if _is_stub(s)], ids=lambda s: s.key)
async def test_stubs_report_not_implemented(spec) -> None:
    module_name, _, attr = spec.target.partition(":")
    cls = getattr(importlib.import_module(module_name), attr)
    kwargs = {"model": "x"} if "model" in inspect.signature(cls).parameters else {}
    provider = cls(spec, **{k: v for k, v in kwargs.items()}, **_stub_extra_kwargs(cls))
    health = await provider.health()
    assert spec.planned and not health.ok and "not implemented" in health.reason


def _stub_extra_kwargs(cls) -> dict:
    params = inspect.signature(cls).parameters
    return {name: None for name in ("options", "context") if name in params
            and params[name].default is inspect.Parameter.empty}


# ------------------------------------------------------------------------------------------------- redaction


def test_redaction_masks_tokens_and_api_keys(caplog) -> None:
    redactor = RedactingFilter(["my-secret-pairing-token-123456"])
    logger = logging.getLogger("clicky.test.redaction")
    handler = logging.Handler()
    records: list[str] = []
    handler.emit = lambda record: records.append(record.getMessage())   # type: ignore[method-assign]
    handler.addFilter(redactor)
    logger.addHandler(handler)
    try:
        logger.warning("token=%s key=%s auth=%s", "my-secret-pairing-token-123456",
                       "sk-ant-api03-abcdefghijklmnopqrstuvwxyz", "Bearer abcdef0123456789")
    finally:
        logger.removeHandler(handler)
    assert records and "my-secret-pairing-token" not in records[0]
    assert "sk-ant-api03" not in records[0] and "abcdef0123456789" not in records[0]
    # uvicorn's DEBUG frame log truncates the hello frame, so the whole token never appears to be matched
    frame = """< TEXT '{"type":"hello","token":"my-secret-pai...ifmf","features":[]}' [143 bytes]"""
    assert redactor.redact(frame) == """< TEXT '{"type":"hello","token":"[REDACTED]","features":[]}' [143 bytes]"""


# --------------------------------------------------------------------------------------------------- hardware


def test_parse_nvidia_smi_gpus() -> None:
    gpus = parse_nvidia_smi_gpus("NVIDIA GeForce RTX 3050 Ti Laptop GPU, 4096, 512\n")
    assert len(gpus) == 1 and gpus[0].memory_total_mib == 4096 and gpus[0].memory_used_mib == 512


def test_parse_nvidia_smi_processes_finds_chrome() -> None:
    text = """
| Processes:                                                                              |
|  GPU   GI   CI        PID   Type   Process name                              GPU Memory |
|=========================================================================================|
|    0   N/A  N/A     12345    C+G   ...m Files\\Google\\Chrome\\Application\\chrome.exe      N/A |
|    0   N/A  N/A      4242      C   ...ocal\\Programs\\Ollama\\ollama.exe     2900MiB |
"""
    processes = parse_nvidia_smi_processes(text)
    names = {p.basename for p in processes}
    assert {"chrome.exe", "ollama.exe"} <= names


@pytest.mark.parametrize(("vram", "tier"), [(None, HardwareTier.CPU), (2.0, HardwareTier.CPU),
                                            (4.0, HardwareTier.GPU_4GB), (8.0, HardwareTier.GPU_8GB),
                                            (24.0, HardwareTier.GPU_24GB)])
def test_tier_for_vram(vram, tier) -> None:
    assert tier_for_vram(vram) is tier


def test_processor_split_matches_ollama_ps() -> None:
    assert processor_split(100, 46) == "54%/46% CPU/GPU"
    assert processor_split(100, 100) == "100% GPU" and processor_split(100, 0) == "100% CPU"


def test_parse_ollama_server_config() -> None:
    line = ('time=2026-11-03T20:48:00 level=INFO source=routes.go:1 msg="server config" '
            'env="map[OLLAMA_CONTEXT_LENGTH:0 OLLAMA_FLASH_ATTENTION:false OLLAMA_HOST:http://127.0.0.1:11434 '
            'OLLAMA_KEEP_ALIVE:5m0s]"')
    env = parse_ollama_server_config("noise\n" + line + "\n")
    assert env is not None
    assert env["OLLAMA_FLASH_ATTENTION"] == "false" and env["OLLAMA_KEEP_ALIVE"] == "5m0s"


# --------------------------------------------------------------------------------------------- schema drift


def test_protocol_schema_is_current() -> None:
    assert export.is_current(), "run: uv run clicky schema export (then npm run gen:protocol in extension/)"
