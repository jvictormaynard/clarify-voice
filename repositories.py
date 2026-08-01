"""Typed configuration and local persistence boundaries.

The desktop application deliberately keeps its JSON files small and human
readable.  This module owns the file format, migrations, and atomic writes so
callers do not need to know where user data lives or how it is encoded.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


CONFIG_SCHEMA_VERSION = 1
STATS_SCHEMA_VERSION = 1
SUPPORTED_PROVIDERS = ("openai", "gemini", "groq")
SUPPORTED_UI_MODES = ("prompt", "transcription")
SUPPORTED_LANGUAGES = ("en", "pt", "es", "de", "ru")
AUDIO_MODEL_ALIASES = {
    ("groq", "whisper large v3 turbo"): "whisper-large-v3-turbo",
    ("groq", "whisper large v3"): "whisper-large-v3",
    ("openai", "whisper 1"): "whisper-1",
}


def environment_defaults(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return startup defaults using the same environment contract as app.py.

    The precedence is persisted configuration, then environment variables,
    then built-in defaults.  Environment values therefore remain useful for
    first-run/headless startup while an existing user's explicit settings are
    not silently replaced on every launch.
    """

    env = os.environ if environment is None else environment
    return {
        "transcription_provider": "gemini",
        "gemini_api_key": env.get("GEMINI_API_KEY", env.get("API_KEY", "")),
        "gemini_base_url": env.get(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"),
        "gemini_model": env.get("GEMINI_MODEL", "gemini-2.5-flash"),
        "openai_api_key": env.get("OPENAI_API_KEY", ""),
        "openai_base_url": env.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "openai_audio_model": env.get("OPENAI_AUDIO_MODEL", "whisper-1"),
        "openai_text_model": env.get("OPENAI_TEXT_MODEL", "gpt-4o-mini"),
        "groq_api_key": env.get("GROQ_API_KEY", ""),
        "groq_base_url": env.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        "groq_audio_model": env.get("GROQ_AUDIO_MODEL", "whisper-large-v3-turbo"),
        "groq_text_model": env.get("GROQ_TEXT_MODEL", "llama-3.3-70b-versatile"),
        "refinement_provider": env.get("REFINEMENT_PROVIDER", ""),
        "refinement_model": env.get("REFINEMENT_MODEL", ""),
        "ui_mode": "prompt",
        "ui_language": "en",
        "autostart": False,
    }


@dataclass(frozen=True)
class ProviderConfig:
    """Settings for one provider, including its endpoint and model IDs."""

    api_key: str = ""
    base_url: str = ""
    audio_model: str = ""
    text_model: str = ""


@dataclass(frozen=True)
class ProviderSelection:
    """The models selected for transcription and text refinement."""

    transcription_provider: str = "gemini"
    refinement_provider: str = "openai"
    refinement_model: str = "gpt-4o-mini"


@dataclass(frozen=True)
class UIPreferences:
    mode: str = "prompt"
    language: str = "en"


@dataclass(frozen=True)
class StartupSettings:
    """Settings that affect launch behavior.

    Windows registry state remains managed by the platform adapter; this
    field is available to callers that need a typed launch preference.
    """

    autostart: bool = False


@dataclass(frozen=True)
class AppConfig:
    """Versioned, typed representation of the persisted application config."""

    schema_version: int = CONFIG_SCHEMA_VERSION
    selection: ProviderSelection = field(default_factory=ProviderSelection)
    gemini: ProviderConfig = field(default_factory=ProviderConfig)
    openai: ProviderConfig = field(default_factory=ProviderConfig)
    groq: ProviderConfig = field(default_factory=ProviderConfig)
    ui: UIPreferences = field(default_factory=UIPreferences)
    startup: StartupSettings = field(default_factory=StartupSettings)

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any] | None,
        defaults: Mapping[str, Any] | None = None,
    ) -> "AppConfig":
        """Build a safe config from legacy flat JSON, ignoring bad fields."""

        source: dict[str, Any] = {}
        if defaults:
            source.update(defaults)
        if isinstance(values, Mapping):
            source.update({key: value for key, value in values.items()})

        def string(key: str, fallback: str = "") -> str:
            value = source.get(key, fallback)
            return value if isinstance(value, str) else fallback

        def choice(key: str, choices: tuple[str, ...], fallback: str) -> str:
            value = string(key, fallback).strip().lower()
            return value if value in choices else fallback

        provider = choice("transcription_provider", SUPPORTED_PROVIDERS, "gemini")
        refinement_provider = choice(
            "refinement_provider", SUPPORTED_PROVIDERS, "openai")
        if not string("refinement_provider"):
            refinement_provider = provider if provider in ("openai", "groq") else "openai"

        provider_defaults = {
            "gemini": {
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "audio_model": "gemini-2.5-flash",
            },
            "openai": {
                "base_url": "https://api.openai.com/v1",
                "audio_model": "whisper-1",
                "text_model": "gpt-4o-mini",
            },
            "groq": {
                "base_url": "https://api.groq.com/openai/v1",
                "audio_model": "whisper-large-v3-turbo",
                "text_model": "llama-3.3-70b-versatile",
            },
        }

        def provider_config(name: str) -> ProviderConfig:
            defaults_for_provider = provider_defaults[name]
            audio_model = string(
                f"{name}_{'model' if name == 'gemini' else 'audio_model'}",
                defaults_for_provider["audio_model"])
            audio_model = AUDIO_MODEL_ALIASES.get(
                (name, audio_model.casefold()), audio_model)
            return ProviderConfig(
                api_key=string(f"{name}_api_key"),
                base_url=string(
                    f"{name}_base_url", defaults_for_provider["base_url"]),
                audio_model=audio_model,
                text_model=string(
                    f"{name}_text_model", defaults_for_provider.get("text_model", "")),
            )

        refinement_model = string("refinement_model")
        if not refinement_model:
            refinement_model = (
                string(f"{refinement_provider}_text_model")
                or ("llama-3.3-70b-versatile" if refinement_provider == "groq"
                    else "gpt-4o-mini")
            )

        mode = choice("ui_mode", SUPPORTED_UI_MODES, "prompt")
        language = choice("ui_language", SUPPORTED_LANGUAGES, "en")
        autostart = source.get("autostart", False)
        if not isinstance(autostart, bool):
            autostart = False

        return cls(
            schema_version=CONFIG_SCHEMA_VERSION,
            selection=ProviderSelection(provider, refinement_provider, refinement_model),
            gemini=provider_config("gemini"),
            openai=provider_config("openai"),
            groq=provider_config("groq"),
            ui=UIPreferences(mode, language),
            startup=StartupSettings(autostart),
        )

    def to_mapping(self) -> dict[str, Any]:
        """Serialize using the existing flat keys plus an explicit version."""

        return {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "transcription_provider": self.selection.transcription_provider,
            "gemini_api_key": self.gemini.api_key,
            "gemini_base_url": self.gemini.base_url,
            "gemini_model": self.gemini.audio_model,
            "openai_api_key": self.openai.api_key,
            "openai_base_url": self.openai.base_url,
            "openai_audio_model": self.openai.audio_model,
            "openai_text_model": self.openai.text_model,
            "groq_api_key": self.groq.api_key,
            "groq_base_url": self.groq.base_url,
            "groq_audio_model": self.groq.audio_model,
            "groq_text_model": self.groq.text_model,
            "refinement_provider": self.selection.refinement_provider,
            "refinement_model": self.selection.refinement_model,
            "ui_mode": self.ui.mode,
            "ui_language": self.ui.language,
            "autostart": self.startup.autostart,
        }

    def to_legacy_mapping(self) -> dict[str, Any]:
        """Return the mapping shape used by the existing UI/provider code."""

        values = self.to_mapping()
        values.pop("schema_version", None)
        values.pop("autostart", None)
        return values


def _version(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _migrate_legacy_to_v1(payload: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(payload)
    migrated["schema_version"] = 1
    return migrated


CONFIG_MIGRATIONS = {0: _migrate_legacy_to_v1}


def migrate_config_payload(payload: Any) -> dict[str, Any]:
    """Apply ordered config migrations; repeated application is a no-op."""

    if not isinstance(payload, Mapping):
        return {"schema_version": CONFIG_SCHEMA_VERSION}
    migrated = dict(payload)
    version = _version(migrated.get("schema_version"))
    # Future files are read in a compatibility mode: known fields are still
    # available, while unknown fields are never trusted or executed.
    if version > CONFIG_SCHEMA_VERSION:
        return migrated
    while version < CONFIG_SCHEMA_VERSION:
        migration = CONFIG_MIGRATIONS.get(version)
        if migration is None:
            break
        migrated = migration(migrated)
        next_version = _version(migrated.get("schema_version"))
        if next_version <= version:
            break
        version = next_version
    migrated["schema_version"] = min(version, CONFIG_SCHEMA_VERSION)
    return migrated


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


class ConfigRepository(ABC):
    """Application-facing configuration persistence interface."""

    @abstractmethod
    def load(self) -> AppConfig:
        raise NotImplementedError

    @abstractmethod
    def save(self, config: AppConfig | Mapping[str, Any]) -> None:
        raise NotImplementedError


class UsageStatsRepository(ABC):
    """Application-facing anonymous usage persistence interface."""

    @abstractmethod
    def load_events(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def append(self, event: Mapping[str, Any]) -> None:
        raise NotImplementedError


class UnsupportedSchemaVersionError(OSError):
    """Raised when saving would downgrade a file from a newer schema."""


class LocalConfigRepository(ConfigRepository):
    """JSON-backed repository for ``%APPDATA%\\ClarifyVoice\\config.json``."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        defaults: Mapping[str, Any] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.defaults = dict(defaults or environment_defaults(environment))
        self._lock = threading.RLock()
        self._future_schema_version: int | None = None

    def load(self) -> AppConfig:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                raw = {}
            version = _version(raw.get("schema_version")) if isinstance(raw, Mapping) else 0
            self._future_schema_version = (
                version if version > CONFIG_SCHEMA_VERSION else None)
            migrated = migrate_config_payload(raw)
            return AppConfig.from_mapping(migrated, self.defaults)

    def save(self, config: AppConfig | Mapping[str, Any]) -> None:
        with self._lock:
            if self._future_schema_version is not None:
                raise UnsupportedSchemaVersionError(
                    f"Cannot save schema version {self._future_schema_version} "
                    f"with supported version {CONFIG_SCHEMA_VERSION}")
            if isinstance(config, AppConfig):
                if config.schema_version > CONFIG_SCHEMA_VERSION:
                    raise UnsupportedSchemaVersionError(
                        f"Cannot save schema version {config.schema_version} "
                        f"with supported version {CONFIG_SCHEMA_VERSION}")
                model = config
            else:
                supplied_version = _version(config.get("schema_version"))
                if supplied_version > CONFIG_SCHEMA_VERSION:
                    raise UnsupportedSchemaVersionError(
                        f"Cannot save schema version {supplied_version} "
                        f"with supported version {CONFIG_SCHEMA_VERSION}")
                model = AppConfig.from_mapping(config, self.defaults)
            _atomic_write_json(self.path, model.to_mapping())


class LocalUsageStatsRepository(UsageStatsRepository):
    """Atomic JSON-backed repository for anonymous usage events."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def load_events(self) -> list[dict[str, Any]]:
        with self._lock:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return []
            if not isinstance(payload, Mapping):
                return []
            events = payload.get("events", [])
            if not isinstance(events, list):
                return []
            return [copy.deepcopy(event) for event in events if isinstance(event, dict)]

    def append(self, event: Mapping[str, Any]) -> None:
        if not isinstance(event, Mapping):
            return
        with self._lock:
            events = self.load_events()
            events.append(copy.deepcopy(dict(event)))
            _atomic_write_json(self.path, {
                "schema_version": STATS_SCHEMA_VERSION,
                "version": STATS_SCHEMA_VERSION,
                "events": events,
            })


# Descriptive aliases for callers that prefer the storage implementation name.
JsonConfigRepository = LocalConfigRepository
JsonUsageStatsRepository = LocalUsageStatsRepository


@dataclass(frozen=True)
class ApplicationRepositories:
    config: ConfigRepository
    usage_stats: UsageStatsRepository
