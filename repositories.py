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

from provider_registry import PROVIDER_IDS, PROVIDER_REGISTRY
from provider_types import ProviderCapability
from hotkey_config import HotkeySettings
from secret_store import (
    SecretStore,
    SecretStoreError,
    SecretStoreUnavailableError,
    create_secret_store,
)


CONFIG_SCHEMA_VERSION = 1
STATS_SCHEMA_VERSION = 1
SUPPORTED_PROVIDERS = PROVIDER_IDS
PROVIDER_SECRET_KEYS = {
    "gemini": "gemini_api_key",
    "openai": "openai_api_key",
    "groq": "groq_api_key",
}
SUPPORTED_UI_MODES = ("prompt", "transcription")
SUPPORTED_LANGUAGES = ("en", "pt", "es", "de", "ru")


def environment_defaults(environment: Mapping[str, str] | None = None) -> dict[str, Any]:
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
        "local_asr_model": env.get("LOCAL_ASR_MODEL", "ggml-small"),
        # Local transcription never sends text to a cloud refinement provider
        # unless the user explicitly opts in from Settings.
        "local_asr_cloud_refinement": False,
        "refinement_provider": env.get("REFINEMENT_PROVIDER", ""),
        "refinement_model": env.get("REFINEMENT_MODEL", ""),
        "ui_mode": "prompt",
        "ui_language": "en",
        "autostart": False,
        # The nested value is intentionally present in the defaults mapping so
        # first-run and legacy flat files both receive the same typed bindings.
        # HotkeySettings.from_mapping() repairs malformed entries one by one.
        "hotkeys": HotkeySettings.defaults().to_mapping(),
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
    local_asr: ProviderConfig = field(default_factory=ProviderConfig)
    ui: UIPreferences = field(default_factory=UIPreferences)
    startup: StartupSettings = field(default_factory=StartupSettings)
    local_asr_cloud_refinement: bool = False
    hotkeys: HotkeySettings = field(default_factory=HotkeySettings.defaults)

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
        requested_refinement_provider = string("refinement_provider").strip().lower()
        refinement_provider = choice(
            "refinement_provider", SUPPORTED_PROVIDERS, "openai")
        refinement_fallback = (
            not requested_refinement_provider
            or requested_refinement_provider not in SUPPORTED_PROVIDERS
            or not PROVIDER_REGISTRY.supports(
                refinement_provider, ProviderCapability.TEXT_GENERATION)
        )
        if refinement_fallback:
            refinement_provider = (
                provider if (
                    PROVIDER_REGISTRY.supports(
                        provider, ProviderCapability.TEXT_GENERATION)
                        and not PROVIDER_REGISTRY.supports(
                        provider, ProviderCapability.MULTIMODAL_AUDIO)
                )
                else "openai")

        provider_defaults = {
            metadata.provider_id: {
                "base_url": metadata.default_base_url,
                "audio_model": metadata.default_audio_model,
                "text_model": metadata.default_text_model,
            }
            for metadata in PROVIDER_REGISTRY.metadata
        }

        def provider_config(name: str) -> ProviderConfig:
            metadata = PROVIDER_REGISTRY.describe(name)
            defaults_for_provider = provider_defaults[name]
            audio_model = string(
                metadata.audio_model_key,
                defaults_for_provider["audio_model"])
            audio_model = PROVIDER_REGISTRY.canonical_audio_model(
                name, audio_model)
            return ProviderConfig(
                api_key=string(f"{name}_api_key"),
                base_url=string(
                    f"{name}_base_url", defaults_for_provider["base_url"]),
                audio_model=audio_model,
                # Gemini historically stores its shared multimodal model only
                # in ``gemini_model``; do not synthesize a new text-model field
                # in the typed legacy adapter.
                text_model=("" if name == "gemini" else string(
                    metadata.text_model_key, defaults_for_provider["text_model"])),
            )

        # A model saved for an invalid/transcription-only provider is not
        # meaningful for the text-capable fallback and must not survive the
        # normalization step.
        refinement_model = "" if refinement_fallback else string("refinement_model")
        if not refinement_model:
            metadata = PROVIDER_REGISTRY.describe(refinement_provider)
            refinement_model = (
                string(metadata.text_model_key) or metadata.default_text_model)

        mode = choice("ui_mode", SUPPORTED_UI_MODES, "prompt")
        language = choice("ui_language", SUPPORTED_LANGUAGES, "en")
        autostart = source.get("autostart", False)
        if not isinstance(autostart, bool):
            autostart = False
        local_asr_cloud_refinement = source.get(
            "local_asr_cloud_refinement", False)
        if not isinstance(local_asr_cloud_refinement, bool):
            local_asr_cloud_refinement = False

        supplied_hotkeys = (
            values.get("hotkeys") if isinstance(values, Mapping) else None)
        hotkey_payload = supplied_hotkeys if supplied_hotkeys is not None else source
        hotkeys = HotkeySettings.from_mapping(hotkey_payload)
        # A short-lived pre-release spelling used the activation mode at the
        # top level. Keep accepting it while writing the versioned nested
        # representation below.
        if "recording_activation_mode" in source:
            try:
                hotkeys = HotkeySettings(
                    hotkeys.hotkeys, source["recording_activation_mode"])
            except ValueError:
                pass

        return cls(
            schema_version=CONFIG_SCHEMA_VERSION,
            selection=ProviderSelection(provider, refinement_provider, refinement_model),
            gemini=provider_config("gemini"),
            openai=provider_config("openai"),
            groq=provider_config("groq"),
            local_asr=provider_config("local_asr"),
            ui=UIPreferences(mode, language),
            startup=StartupSettings(autostart),
            local_asr_cloud_refinement=local_asr_cloud_refinement,
            hotkeys=hotkeys,
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
            "local_asr_model": self.local_asr.audio_model,
            "local_asr_cloud_refinement": self.local_asr_cloud_refinement,
            "refinement_provider": self.selection.refinement_provider,
            "refinement_model": self.selection.refinement_model,
            "ui_mode": self.ui.mode,
            "ui_language": self.ui.language,
            "autostart": self.startup.autostart,
            "hotkeys": self.hotkeys.to_mapping(),
        }

    def to_legacy_mapping(self) -> dict[str, Any]:
        """Return the mapping shape used by the existing UI/provider code."""

        values = self.to_mapping()
        values.pop("schema_version", None)
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


def _read_json_mapping(path: Path) -> dict[str, Any] | None:
    """Read a JSON object for a pre-write schema check, if one exists."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


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


def _ensure_supported_schema(
    path: Path,
    supported_version: int,
    legacy_version_key: str | None = None,
) -> int | None:
    """Inspect the current file immediately before a replacement write."""
    payload = _read_json_mapping(path)
    if payload is None:
        return None
    versions = [_version(payload.get("schema_version"))]
    if legacy_version_key:
        versions.append(_version(payload.get(legacy_version_key)))
    version = max(versions)
    if version > supported_version:
        raise UnsupportedSchemaVersionError(
            f"Cannot save schema version {version} with supported version "
            f"{supported_version}")
    return version


class LocalConfigRepository(ConfigRepository):
    """JSON settings plus a provider-keyed credential-store boundary."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        defaults: Mapping[str, Any] | None = None,
        environment: Mapping[str, str] | None = None,
        secret_store: SecretStore | None = None,
    ) -> None:
        self.path = Path(path)
        self.environment = os.environ if environment is None else environment
        self.defaults = dict(defaults or environment_defaults(self.environment))
        secret_stem = ("secrets" if self.path.name == "config.json"
                       else f"{self.path.stem}.secrets")
        self.secret_store = secret_store or create_secret_store(
            self.path.parent, filename_stem=secret_stem)
        self._lock = threading.RLock()

    def _environment_secret(self, provider: str) -> str | None:
        names = (("GEMINI_API_KEY", "API_KEY") if provider == "gemini"
                 else (f"{provider.upper()}_API_KEY",))
        for name in names:
            value = self.environment.get(name)
            if isinstance(value, str) and value:
                return value
        return None

    def _restore_secrets(self, previous: Mapping[str, str | None]) -> None:
        for provider, value in previous.items():
            try:
                if value:
                    self.secret_store.set(provider, value)
                else:
                    self.secret_store.delete(provider)
            except Exception:
                # The original write error remains the actionable result. A
                # later load still preserves any untouched plaintext legacy key.
                pass

    def _load_runtime_mapping(
        self,
        raw: Mapping[str, Any],
        migrated: Mapping[str, Any],
    ) -> dict[str, Any]:
        runtime = dict(migrated)
        removable: set[str] = set()
        future_schema = _version(
            migrated.get("schema_version")) > CONFIG_SCHEMA_VERSION

        for provider, key in PROVIDER_SECRET_KEYS.items():
            legacy = raw.get(key)
            legacy = legacy if isinstance(legacy, str) and legacy else None
            stored = None
            store_available = True
            try:
                stored = self.secret_store.get(provider)
            except SecretStoreError:
                store_available = False
            except Exception:
                # Third-party or injected backends must not be able to expose
                # a raw exception (which may contain credential material).
                store_available = False

            if legacy and store_available and not future_schema:
                try:
                    if stored is None:
                        self.secret_store.set(provider, legacy)
                        stored = self.secret_store.get(provider)
                        if stored != legacy:
                            stored = None
                    if stored == legacy:
                        removable.add(key)
                except (SecretStoreError, ValueError):
                    stored = None
                except Exception:
                    # Preserve the plaintext source when an injected backend
                    # fails outside the SecretStore error hierarchy.
                    stored = None

            runtime[key] = self._environment_secret(provider) or stored or legacy or ""

        if removable:
            sanitized = dict(migrated)
            for key in removable:
                sanitized.pop(key, None)
            try:
                _ensure_supported_schema(self.path, CONFIG_SCHEMA_VERSION)
                _atomic_write_json(self.path, sanitized)
            except OSError:
                # Migration remains recoverable: the plaintext source is still
                # present and the verified secure copy can be retried next load.
                pass
        return runtime

    def load(self) -> AppConfig:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                raw = {}
            migrated = migrate_config_payload(raw)
            raw_mapping = raw if isinstance(raw, Mapping) else {}
            runtime = self._load_runtime_mapping(raw_mapping, migrated)
            return AppConfig.from_mapping(runtime, self.defaults)

    def save(self, config: AppConfig | Mapping[str, Any]) -> None:
        with self._lock:
            _ensure_supported_schema(self.path, CONFIG_SCHEMA_VERSION)
            current_payload = _read_json_mapping(self.path) or {}
            if isinstance(config, AppConfig):
                if config.schema_version > CONFIG_SCHEMA_VERSION:
                    raise UnsupportedSchemaVersionError(
                        f"Cannot save schema version {config.schema_version} "
                        f"with supported version {CONFIG_SCHEMA_VERSION}")
                model = config
                supplied_keys = set(PROVIDER_SECRET_KEYS.values())
            else:
                supplied_version = _version(config.get("schema_version"))
                if supplied_version > CONFIG_SCHEMA_VERSION:
                    raise UnsupportedSchemaVersionError(
                        f"Cannot save schema version {supplied_version} "
                        f"with supported version {CONFIG_SCHEMA_VERSION}")
                model = AppConfig.from_mapping(config, self.defaults)
                supplied_keys = set(config)

            values = model.to_mapping()
            changes: dict[str, str] = {}
            preserve_legacy: set[str] = set()
            for provider, key in PROVIDER_SECRET_KEYS.items():
                if key not in supplied_keys:
                    continue
                value = str(values.get(key, ""))
                environment_value = self._environment_secret(provider)
                if environment_value and value == environment_value:
                    # Environment credentials are runtime-only overrides. A
                    # normal settings save must never copy them into storage.
                    # A different, explicitly submitted value is user intent
                    # and must replace the stored credential.
                    #
                    # If a previous load could not migrate a legacy key, do
                    # not remove that recoverable source merely because the
                    # environment currently masks it. A later save without
                    # the override can retry the migration.
                    if (isinstance(current_payload.get(key), str)
                            and current_payload.get(key)):
                        preserve_legacy.add(key)
                    continue
                changes[provider] = value

            previous: dict[str, str | None] = {}
            try:
                for provider in changes:
                    previous[provider] = self.secret_store.get(provider)
                for provider, value in changes.items():
                    if value:
                        self.secret_store.set(provider, value)
                        if self.secret_store.get(provider) != value:
                            raise SecretStoreError(
                                "The credential store failed its verification")
                    else:
                        self.secret_store.delete(provider)
                        if self.secret_store.get(provider) is not None:
                            raise SecretStoreError(
                                "The credential store failed its verification")

                for key in PROVIDER_SECRET_KEYS.values():
                    values.pop(key, None)
                # A partial settings write must not destroy an unmigrated
                # plaintext credential. Full application saves supply every
                # key and therefore either verify secret storage or fail.
                for key in PROVIDER_SECRET_KEYS.values():
                    legacy = current_payload.get(key)
                    if ((key not in supplied_keys or key in preserve_legacy)
                            and isinstance(legacy, str)
                            and legacy):
                        values[key] = legacy
                _atomic_write_json(self.path, values)
            except SecretStoreError:
                self._restore_secrets(previous)
                raise
            except (OSError, ValueError):
                self._restore_secrets(previous)
                raise
            except Exception:
                self._restore_secrets(previous)
                raise SecretStoreUnavailableError(
                    "The credential store could not be updated") from None


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
            _ensure_supported_schema(
                self.path, STATS_SCHEMA_VERSION, legacy_version_key="version")
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
