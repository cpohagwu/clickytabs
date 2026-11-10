from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from clicky.capabilities.kinds import Capability, HardwareTier, Profile
from clicky.capabilities.registry import effective_choices, resolve_all
from clicky.config import Settings, clicky_home, normalize_ollama_host
from tests.conftest import DEV_ID

EXAMPLE_TOML = Path(__file__).resolve().parents[1] / "clicky.toml.example"


def test_home_comes_from_env(tmp_path) -> None:
    assert clicky_home() == tmp_path.resolve()
    assert Settings().home == tmp_path.resolve()


def test_csv_env_lists(monkeypatch) -> None:
    monkeypatch.setenv("CLICKY_ALLOWED_EXTENSION_IDS", f"{DEV_ID}, other")
    monkeypatch.setenv("CLICKY_TRUSTED_HOSTS", "127.0.0.1")
    s = Settings()
    assert s.allowed_extension_ids == [DEV_ID, "other"]
    assert s.trusted_hosts == ["127.0.0.1"]


def test_env_beats_dotenv_beats_toml(tmp_path, monkeypatch) -> None:
    (tmp_path / "clicky.toml").write_text('port = 1111\nlog_level = "DEBUG"\nprofile = "cloud"\n', encoding="utf-8")
    (tmp_path / ".env").write_text("CLICKY_PORT=2222\nANTHROPIC_API_KEY=sk-ant-from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("CLICKY_PROFILE", "fake")
    s = Settings()
    assert s.port == 2222 and s.log_level == "DEBUG" and s.profile is Profile.FAKE
    assert s.secret_value("anthropic_api_key") == "sk-ant-from-dotenv"


def test_profiles_never_come_from_env(monkeypatch) -> None:
    monkeypatch.setenv("CLICKY_PROFILES", '{"local": {"capabilities": {"brain": {"provider": "fake"}}}}')
    assert Settings().profiles == {}


@pytest.mark.parametrize("profile", list(Profile))
def test_example_toml_validates_for_every_profile(tmp_path, monkeypatch, profile: Profile) -> None:
    shutil.copy(EXAMPLE_TOML, tmp_path / "clicky.toml")
    monkeypatch.setenv("CLICKY_PROFILE", profile.value)
    s = Settings()
    assert s.allowed_extension_ids == [DEV_ID]
    assert set(resolve_all(s, HardwareTier.GPU_4GB)) == set(Capability)


def test_fake_profile_ignores_overrides(tmp_path, monkeypatch) -> None:
    (tmp_path / "clicky.toml").write_text(
        '[profiles.fake.capabilities.brain]\nprovider = "ollama"\nmodel = "qwen3.5:4b"\n', encoding="utf-8")
    monkeypatch.setenv("CLICKY_PROFILE", "fake")
    choices = effective_choices(Settings(), HardwareTier.GPU_4GB)
    assert all(choice.provider == "fake" for choice, _ in choices.values())


def test_overrides_replace_presets_atomically(tmp_path) -> None:
    (tmp_path / "clicky.toml").write_text(
        'profile = "local"\n[profiles.local.capabilities.brain]\nprovider = "ollama"\nmodel = "qwen2.5vl:3b"\n',
        encoding="utf-8")
    choice, source = effective_choices(Settings(), HardwareTier.GPU_4GB)[Capability.BRAIN]
    assert (choice.provider, choice.model, choice.fallbacks) == ("ollama", "qwen2.5vl:3b", [])
    assert source.startswith("override")


def test_local_gpu4gb_preset_shares_one_ollama_model() -> None:
    choices = effective_choices(Settings(profile="local"), HardwareTier.GPU_4GB)
    roles = (Capability.BRAIN, Capability.CODEGEN, Capability.SUMMARIZER, Capability.AGENT_LLM)
    assert {choices[c][0].model for c in roles} == {"qwen3.5:4b"}


def test_every_profile_and_tier_resolves() -> None:
    for profile in Profile:
        for tier in HardwareTier:
            if tier is HardwareTier.AUTO:
                continue
            chains = resolve_all(Settings(profile=profile), tier)
            assert set(chains) == set(Capability), (profile, tier)


def test_token_bootstrap_and_rotate() -> None:
    s = Settings()
    first = s.resolved_token()
    assert len(first) >= 32 and s.token_path.read_text(encoding="utf-8").strip() == first
    assert Settings().resolved_token() == first
    rotated = s.rotate_token()
    assert rotated != first and Settings().resolved_token() == rotated
    assert Settings(auth_token="fixed").resolved_token() == "fixed"


@pytest.mark.parametrize(("raw", "expected"), [
    (None, "http://127.0.0.1:11434"), ("0.0.0.0:11434", "http://127.0.0.1:11434"),
    ("localhost", "http://localhost:11434"), ("http://h:1234/", "http://h:1234"),
])
def test_normalize_ollama_host(raw: str | None, expected: str) -> None:
    assert normalize_ollama_host(raw) == expected
