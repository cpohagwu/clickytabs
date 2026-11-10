"""What the options page chose: the profile, and per profile one provider (model, voice) per capability.

Stored in CLICKY_HOME/.clicky/ui-settings.json (written atomically, owner-only where the OS supports it), apart from
clicky.toml so the backend never rewrites the user's own config file. Precedence:

- profile: CLICKY_PROFILE (which `clicky serve --profile` also sets) pins it; otherwise this file; otherwise
  clicky.toml or .env; otherwise "local".
- capability: this file's provider first, then the clicky.toml override or the preset chain as its fallbacks, so a
  provider that turns out unhealthy still falls back. Unlike clicky.toml overrides it applies to the fake profile too
  (the file lives in CLICKY_HOME, which tests make temporary).

Only provider, model and a few safe options (voice, effort, speed: see `UI_OPTION_KEYS`) can be stored here.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from clicky.capabilities.kinds import Capability, Profile
from clicky.config import ProviderChoice, Settings
from clicky.protocol.events import ProviderChoiceWire

log = logging.getLogger(__name__)

UI_SETTINGS_FILE = "ui-settings.json"


class UiSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Profile | None = None
    capabilities: dict[Profile, dict[Capability, ProviderChoiceWire]] = Field(default_factory=dict)

    def choice(self, profile: Profile, capability: Capability) -> ProviderChoiceWire | None:
        return self.capabilities.get(profile, {}).get(capability)


def ui_settings_path(settings: Settings) -> Path:
    return settings.data_dir / UI_SETTINGS_FILE


def load_ui_settings(settings: Settings) -> UiSettings:
    """The stored choices; a missing file means none, an unreadable or invalid one is ignored with a warning."""
    path = ui_settings_path(settings)
    try:
        return UiSettings.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return UiSettings()
    except (OSError, ValueError) as exc:
        log.warning("ignoring %s: %s", path, str(exc).splitlines()[0])
        return UiSettings()


def save_ui_settings(settings: Settings, ui: UiSettings) -> None:
    path = ui_settings_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(ui.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def profile_pinned() -> bool:
    """True when the environment decides the profile (CLICKY_PROFILE or `clicky serve --profile`)."""
    return bool(os.environ.get("CLICKY_PROFILE", "").strip())


def with_ui_profile(settings: Settings, ui: UiSettings) -> Settings:
    """`settings` with the options page's profile applied, unless the environment pins the profile."""
    if ui.profile is None or ui.profile is settings.profile or profile_pinned():
        return settings
    return settings.model_copy(update={"profile": ui.profile})


def ui_choice(ui: UiSettings, profile: Profile, capability: Capability) -> ProviderChoice | None:
    wire = ui.choice(profile, capability)
    if wire is None:
        return None
    return ProviderChoice(provider=wire.provider, model=wire.model, options=dict(wire.options))
