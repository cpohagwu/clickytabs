"""Settings.

Resolution order (highest first): init kwargs > CLICKY_* environment > CLICKY_HOME/.env > CLICKY_HOME/clicky.toml.
`profiles` and `ollama` are read from clicky.toml (or init) only, so an environment variable can never half-merge a
ProviderChoice. Provider API keys use their standard names (ANTHROPIC_API_KEY, ...) and are passed explicitly to SDK
clients: values from .env are never exported to os.environ.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Annotated, Any

import platformdirs
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from clicky.capabilities.kinds import Capability, HardwareTier, Profile

DEFAULT_PORT = 8765
_SOURCE_ROOT = Path(__file__).resolve().parents[2]          # backend/ when running from source (uv editable install)


def clicky_home() -> Path:
    """One directory for .env, clicky.toml and .clicky/ (token, caches) - independent of the working directory."""
    if env := os.environ.get("CLICKY_HOME"):
        return Path(env).expanduser().resolve()
    if (_SOURCE_ROOT / "pyproject.toml").is_file():
        return _SOURCE_ROOT
    return Path(platformdirs.user_data_dir("ClickyTabs", appauthor=False))


def _default_extension_ids() -> list[str]:
    """Out of the box, allow only the pinned dev extension ID committed in the repo (if running from source)."""
    id_file = clicky_home().parent / "extension" / "keys" / "dev-extension-id.txt"
    try:
        value = id_file.read_text(encoding="utf-8").strip()
    except OSError:
        return []
    return [value] if value else []


def normalize_ollama_host(value: str | None) -> str:
    """Accept OLLAMA_HOST-style values ("0.0.0.0:11434", "localhost", "http://h:1234/") and return a client URL."""
    if not value or not value.strip():
        return "http://127.0.0.1:11434"
    url = value.strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    url = url.replace("://0.0.0.0", "://127.0.0.1")
    if url.count(":") == 1:                               # scheme only, no port
        url += ":11434"
    return url


class RunnerOptions(BaseModel):
    """Ollama runner options. Defined once per model and shared by every role, otherwise Ollama reloads the model."""

    model_config = ConfigDict(extra="forbid")

    num_ctx: int | None = None
    num_batch: int | None = None
    num_gpu: int | None = None
    num_thread: int | None = None


class OllamaSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(default_factory=lambda: normalize_ollama_host(os.environ.get("OLLAMA_HOST")))
    runner: dict[str, RunnerOptions] = Field(default_factory=dict)   # keyed by model tag, e.g. "qwen3.5:4b"
    request_timeout_s: float = 300.0

    @field_validator("host", mode="before")
    @classmethod
    def _normalize_host(cls, v: Any) -> Any:
        return normalize_ollama_host(v) if isinstance(v, str) else v


class ProviderChoice(BaseModel):
    """A provider for one capability. Replaced as a whole by higher-priority config, never merged field-by-field."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)       # validated by the adapter's own options model
    fallbacks: list[ProviderChoice] = Field(default_factory=list)


class ProfileOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capabilities: dict[Capability, ProviderChoice] = Field(default_factory=dict)


class _SkipNestedMixin:
    _skip = frozenset({"profiles", "ollama"})

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        if field_name in self._skip:
            return None, field_name, False
        return super().get_field_value(field, field_name)  # type: ignore[misc]


class _EnvSource(_SkipNestedMixin, EnvSettingsSource):
    pass


class _DotEnvSource(_SkipNestedMixin, DotEnvSettingsSource):
    pass


def _secret(*names: str) -> Any:
    return Field(default=None, validation_alias=AliasChoices(*names))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLICKY_", extra="ignore", validate_by_name=True)

    home: Path = Field(default_factory=clicky_home)
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    log_level: str = "INFO"
    hello_timeout_s: float = 5.0
    auth_token: SecretStr | None = None
    trusted_hosts: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["127.0.0.1", "localhost"])
    allowed_extension_ids: Annotated[list[str], NoDecode] = Field(default_factory=_default_extension_ids)
    profile: Profile = Profile.LOCAL
    hardware_tier: HardwareTier = HardwareTier.AUTO
    profiles: dict[str, ProfileOverrides] = Field(default_factory=dict)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    # Browser tasks: the agent's browser is headed by default (you can watch it work); tests run it headless.
    automation_headless: bool = False
    automation_browser: str | None = None          # Chrome/Chromium executable; None = browser-use finds one
    automation_browser_args: Annotated[list[str], NoDecode] = Field(default_factory=list)   # extra Chromium flags
    automation_max_steps: int = Field(default=40, ge=1, le=200)

    anthropic_api_key: SecretStr | None = _secret("ANTHROPIC_API_KEY", "anthropic_api_key")
    openai_api_key: SecretStr | None = _secret("OPENAI_API_KEY", "openai_api_key")
    openai_base_url: str | None = _secret("OPENAI_BASE_URL", "openai_base_url")
    deepgram_api_key: SecretStr | None = _secret("DEEPGRAM_API_KEY", "deepgram_api_key")
    elevenlabs_api_key: SecretStr | None = _secret("ELEVENLABS_API_KEY", "elevenlabs_api_key")

    @field_validator("trusted_hosts", "allowed_extension_ids", "automation_browser_args", mode="before")
    @classmethod
    def _split_csv(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [part.strip() for part in v.split(",") if part.strip()]
        return v

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        home = clicky_home()
        return (
            init_settings,
            _EnvSource(settings_cls),
            _DotEnvSource(settings_cls, env_file=home / ".env"),
            TomlConfigSettingsSource(settings_cls, toml_file=home / "clicky.toml"),
            file_secret_settings,
        )

    # ------------------------------------------------------------------------------------------------- helpers

    @property
    def data_dir(self) -> Path:
        return self.home / ".clicky"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "auth_token"

    def resolved_token(self) -> str:
        """CLICKY_AUTH_TOKEN if set, else the persisted token, else a freshly generated one (persisted)."""
        if self.auth_token is not None and self.auth_token.get_secret_value():
            return self.auth_token.get_secret_value()
        try:
            token = self.token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            token = ""
        return token or self.rotate_token()

    def rotate_token(self) -> str:
        token = secrets.token_urlsafe(32)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.token_path.with_suffix(".tmp")
        tmp.write_text(token + "\n", encoding="utf-8")
        os.replace(tmp, self.token_path)
        try:
            os.chmod(self.token_path, 0o600)
        except OSError:
            pass
        return token

    def overrides_for(self, profile: Profile | None = None) -> dict[Capability, ProviderChoice]:
        """Per-profile capability overrides from clicky.toml. The fake profile ignores them (hermetic tests/e2e)."""
        active = profile or self.profile
        if active is Profile.FAKE:
            return {}
        return dict(self.profiles.get(active.value, ProfileOverrides()).capabilities)

    def secret_value(self, name: str | None) -> str | None:
        if not name:
            return None
        value = getattr(self, name, None)
        if isinstance(value, SecretStr):
            return value.get_secret_value() or None
        return value or None


def get_settings() -> Settings:
    return Settings()
