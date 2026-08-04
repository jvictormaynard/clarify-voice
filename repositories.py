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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from provider_registry import PROVIDER_IDS, PROVIDER_REGISTRY
from provider_types import ProviderCapability, ProviderError
from hotkey_config import HotkeySettings
from secret_store import (
    SecretStore,
    SecretStoreError,
    SecretStoreUnavailableError,
    create_secret_store,
)
from workflow_config import (
    DEFAULT_WORKFLOW_PROMPTS,
    WORKFLOW_SCOPES,
    WorkflowConfig,
    WorkflowConfigurationError,
    WorkflowRoute,
    WorkflowScope,
    WorkflowTestResult,
    normalize_workflow_scope,
    test_workflow_configuration,
    validate_workflow_config,
    validate_workflow_route,
)

__all__ = [
    "AppConfig", "ApplicationRepositories", "ConfigRepository",
    "LocalConfigRepository", "LocalUsageStatsRepository", "ProviderConfig",
    "ProviderSelection", "StartupSettings", "UIPreferences",
    "WorkflowConfig", "WorkflowConfigurationError", "WorkflowRoute",
    "WorkflowScope", "WorkflowTestResult", "environment_defaults",
    "migrate_config_payload", "normalize_workflow_scope",
    "test_workflow_configuration", "validate_workflow_config",
    "validate_workflow_route",
]


CONFIG_SCHEMA_VERSION = 2
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
    workflows: WorkflowConfig = field(default_factory=WorkflowConfig)

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any] | None,
        defaults: Mapping[str, Any] | None = None,
    ) -> "AppConfig":
        """Build a safe config from flat or workflow-scoped JSON.

        The flat fields remain the compatibility source for the current UI,
        while the nested ``workflows`` object is authoritative whenever a
        route explicitly supplies a value.  This lets the migration land
        without forcing a product UI rewrite in the same change.
        """

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
        route_defaults = WorkflowConfig(
            transcription=WorkflowRoute(
                provider_id=provider,
                model_id=PROVIDER_REGISTRY.canonical_audio_model(
                    provider,
                    provider_config(provider).audio_model,
                ),
                prompt=DEFAULT_WORKFLOW_PROMPTS[
                    WorkflowScope.TRANSCRIPTION.value
                ],
            ),
            refinement=WorkflowRoute(
                provider_id=refinement_provider,
                model_id=refinement_model,
                prompt=DEFAULT_WORKFLOW_PROMPTS[WorkflowScope.REFINEMENT.value],
            ),
            rewrite=WorkflowRoute(
                provider_id=refinement_provider,
                model_id=refinement_model,
                prompt=DEFAULT_WORKFLOW_PROMPTS[WorkflowScope.REWRITE.value],
            ),
            translation=WorkflowRoute(
                provider_id=refinement_provider,
                model_id=refinement_model,
                prompt=DEFAULT_WORKFLOW_PROMPTS[WorkflowScope.TRANSLATION.value],
            ),
            local_asr_refinement=WorkflowRoute(
                provider_id=refinement_provider,
                model_id=refinement_model,
                prompt=DEFAULT_WORKFLOW_PROMPTS[
                    WorkflowScope.LOCAL_ASR_REFINEMENT.value
                ],
                enabled=local_asr_cloud_refinement,
            ),
        )
        workflow_values = source.get("workflows")
        workflows = WorkflowConfig.from_mapping(
            workflow_values if isinstance(workflow_values, Mapping) else None,
            defaults=route_defaults,
        )
        # Canonicalize persisted audio aliases while leaving unsupported
        # combinations for the explicit apply/test validation boundary.
        try:
            transcription = workflows.transcription
            workflows = workflows.with_route(
                WorkflowScope.TRANSCRIPTION,
                replace(
                    transcription,
                    model_id=PROVIDER_REGISTRY.canonical_audio_model(
                        transcription.provider_id, transcription.model_id
                    ),
                ),
            )
        except (ProviderError, ValueError):
            pass

        workflow_mapping: dict[str, Any] = {}
        if isinstance(workflow_values, Mapping):
            # Mirror WorkflowConfig.from_mapping() here so compatibility
            # fields are derived from the same effective route regardless of
            # whether callers used canonical scopes or a documented alias.
            canonical_values: dict[str, Any] = {}
            alias_values: dict[str, Any] = {}
            for raw_scope, route in workflow_values.items():
                normalized = normalize_workflow_scope(raw_scope)
                if normalized not in WORKFLOW_SCOPES:
                    continue
                raw_value = (
                    raw_scope.value
                    if isinstance(raw_scope, WorkflowScope)
                    else str(raw_scope or "")
                ).strip().lower()
                if raw_value == normalized:
                    canonical_values[normalized] = route
                else:
                    alias_values[normalized] = route
            workflow_mapping.update(alias_values)
            workflow_mapping.update(canonical_values)
        effective_transcription_provider = (
            workflows.transcription.provider_id
            if "transcription" in workflow_mapping else provider
        )
        effective_refinement_provider = (
            workflows.refinement.provider_id
            if "refinement" in workflow_mapping else refinement_provider
        )
        effective_refinement_model = (
            workflows.refinement.model_id
            if "refinement" in workflow_mapping else refinement_model
        )
        if ("local_asr_refinement" in workflow_mapping
                and isinstance(workflows.local_asr_refinement.enabled, bool)):
            local_asr_cloud_refinement = workflows.local_asr_refinement.enabled

        provider_configs = {
            name: provider_config(name) for name in PROVIDER_REGISTRY.provider_ids
        }
        if "transcription" in workflow_mapping:
            route = workflows.transcription
            selected_config = provider_configs.get(route.provider_id)
            route_model = str(route.model_id or "").strip()
            if selected_config is not None and route_model:
                # Keep the flat compatibility field consumed by the current
                # runtime in lockstep with a nested route's selected model.
                # Otherwise `audio_model_from_legacy()` would silently use
                # the provider default after a valid nested route is applied.
                provider_configs[route.provider_id] = replace(
                    selected_config, audio_model=route_model)

        return cls(
            schema_version=CONFIG_SCHEMA_VERSION,
            selection=ProviderSelection(
                effective_transcription_provider,
                effective_refinement_provider,
                effective_refinement_model,
            ),
            gemini=provider_configs["gemini"],
            openai=provider_configs["openai"],
            groq=provider_configs["groq"],
            local_asr=provider_configs["local_asr"],
            ui=UIPreferences(mode, language),
            startup=StartupSettings(autostart),
            local_asr_cloud_refinement=local_asr_cloud_refinement,
            hotkeys=hotkeys,
            workflows=workflows,
        )

    def to_mapping(self) -> dict[str, Any]:
        """Serialize nested workflow routes plus legacy compatibility fields."""

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
            "workflows": self.workflows.to_mapping(),
        }

    def to_legacy_mapping(self) -> dict[str, Any]:
        """Return the mapping shape used by the existing UI/provider code."""

        values = self.to_mapping()
        values.pop("schema_version", None)
        return values

    def synchronize_legacy_routes(
        self,
        legacy_config: "AppConfig",
        changed_keys: set[str],
        *,
        preserve_endpoint_scopes: set[str] | frozenset[str] = frozenset(),
    ) -> "AppConfig":
        """Reflect changed flat UI fields without erasing custom routes.

        The legacy desktop surface edits a mapping in place and does not yet
        know about ``workflows``.  Repository saves pass the previous on-disk
        flat values to this method, so a route is synchronized only when the
        corresponding flat value actually changed.  A typed ``AppConfig``
        save remains authoritative for fully independent workflow routes.
        """
        workflows = self.workflows
        selection = self.selection
        provider_updates: dict[str, ProviderConfig] = {}
        selected_audio_key = PROVIDER_REGISTRY.describe(
            legacy_config.selection.transcription_provider).audio_model_key
        if {"transcription_provider", selected_audio_key} & changed_keys:
            current = workflows.transcription
            transcription_route = legacy_config.workflow(
                WorkflowScope.TRANSCRIPTION)
            endpoint = current.custom_endpoint
            if (current.provider_id != transcription_route.provider_id
                    and WorkflowScope.TRANSCRIPTION.value
                    not in preserve_endpoint_scopes):
                endpoint = ""
            workflows = workflows.with_route(
                WorkflowScope.TRANSCRIPTION,
                replace(
                    current,
                    provider_id=transcription_route.provider_id,
                    model_id=transcription_route.model_id,
                    custom_endpoint=endpoint,
                ),
            )
            selection = replace(
                selection,
                transcription_provider=(
                    legacy_config.selection.transcription_provider),
            )
            selected_config = getattr(
                self, legacy_config.selection.transcription_provider, None)
            route_model = str(transcription_route.model_id or "").strip()
            if isinstance(selected_config, ProviderConfig) and route_model:
                # Keep flat consumers in lockstep with the route after a
                # legacy provider/model edit. Without this, a stale nested
                # route can overwrite the selected provider's audio model
                # while synchronization updates only the typed route.
                provider_updates[
                    legacy_config.selection.transcription_provider
                ] = replace(selected_config, audio_model=route_model)
        if {"refinement_provider", "refinement_model"} & changed_keys:
            previous_shared = workflows.refinement
            legacy_route = legacy_config.workflow(WorkflowScope.REFINEMENT)
            for scope in (
                    WorkflowScope.REFINEMENT.value,
                    WorkflowScope.REWRITE.value,
                    WorkflowScope.TRANSLATION.value,
                    WorkflowScope.LOCAL_ASR_REFINEMENT.value):
                current = workflows[scope]
                if (current.provider_id == previous_shared.provider_id
                        and current.model_id == previous_shared.model_id):
                    # A custom endpoint is part of an independently selected
                    # route.  Do not move that route to a different provider
                    # while retaining a provider-specific proxy URL.  The
                    # primary legacy refinement route remains synchronized,
                    # but its endpoint is cleared when the provider changes
                    # so the route cannot combine incompatible settings. A
                    # model-only edit keeps the same provider-specific proxy.
                    if (current.custom_endpoint
                            and scope != WorkflowScope.REFINEMENT.value):
                        continue
                    endpoint = current.custom_endpoint
                    if (scope == WorkflowScope.REFINEMENT.value
                            and current.provider_id
                            != legacy_route.provider_id
                            and scope not in preserve_endpoint_scopes):
                        endpoint = ""
                    workflows = workflows.with_route(
                        scope,
                        replace(
                            current,
                            provider_id=legacy_route.provider_id,
                            model_id=legacy_route.model_id,
                            custom_endpoint=endpoint,
                        ),
                    )
            selection = replace(
                selection,
                refinement_provider=legacy_config.selection.refinement_provider,
                refinement_model=legacy_config.selection.refinement_model,
            )
        if "local_asr_cloud_refinement" in changed_keys:
            workflows = workflows.with_route(
                WorkflowScope.LOCAL_ASR_REFINEMENT,
                replace(
                    workflows.local_asr_refinement,
                    enabled=legacy_config.local_asr_cloud_refinement,
                ),
            )
        return replace(
            self,
            selection=selection,
            workflows=workflows,
            **provider_updates,
            local_asr_cloud_refinement=(
                legacy_config.local_asr_cloud_refinement
                if "local_asr_cloud_refinement" in changed_keys
                else self.local_asr_cloud_refinement),
        )

    def workflow(self, scope: WorkflowScope | str) -> WorkflowRoute:
        return self.workflows.route(scope)

    def diagnostic_mapping(self) -> dict[str, Any]:
        """Return effective routing metadata without prompts or credentials."""
        return {
            "schema_version": self.schema_version,
            "workflows": self.workflows.diagnostic_mapping(),
        }

    def reset_workflow(
        self,
        scope: WorkflowScope | str,
        *,
        registry=PROVIDER_REGISTRY,
    ) -> "AppConfig":
        """Reset one route to the current provider/model defaults."""
        normalized = normalize_workflow_scope(scope)
        if normalized not in WORKFLOW_SCOPES:
            raise WorkflowConfigurationError(
                normalized, f"Unknown workflow scope: {normalized}", field="scope"
            )
        selection = self.selection
        if normalized == WorkflowScope.TRANSCRIPTION.value:
            provider_id = selection.transcription_provider
            metadata = registry.describe(provider_id)
            route = WorkflowRoute(
                provider_id=provider_id,
                model_id=registry.canonical_audio_model(
                    provider_id, metadata.default_audio_model
                ),
                prompt=DEFAULT_WORKFLOW_PROMPTS[normalized],
            )
        else:
            provider_id = selection.refinement_provider
            metadata = registry.describe(provider_id)
            route = WorkflowRoute(
                provider_id=provider_id,
                model_id=metadata.default_text_model,
                prompt=DEFAULT_WORKFLOW_PROMPTS[normalized],
                enabled=(
                    self.local_asr_cloud_refinement
                    if normalized == WorkflowScope.LOCAL_ASR_REFINEMENT.value
                    else True
                ),
            )
        workflows = self.workflows.with_route(normalized, route)
        return replace(self, workflows=workflows,
                       local_asr_cloud_refinement=(
                           route.enabled
                           if normalized == WorkflowScope.LOCAL_ASR_REFINEMENT.value
                           else self.local_asr_cloud_refinement))


def _version(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _migrate_legacy_to_v1(payload: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(payload)
    migrated["schema_version"] = 1
    return migrated


def _migrate_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """Split the shared legacy refinement route into workflow scopes."""
    migrated = dict(payload)
    existing = migrated.get("workflows")
    workflows: dict[str, Any] = {}
    if isinstance(existing, Mapping):
        # Canonical scope names must win over aliases when both are present;
        # otherwise a generated canonical default below can hide an authored
        # route such as ``dictation`` or ``text_refinement``.
        canonical_values: dict[str, Any] = {}
        alias_values: dict[str, Any] = {}
        for raw_scope, route in existing.items():
            normalized = normalize_workflow_scope(raw_scope)
            if normalized not in WORKFLOW_SCOPES:
                workflows[str(raw_scope)] = route
                continue
            raw_value = (
                raw_scope.value
                if isinstance(raw_scope, WorkflowScope)
                else str(raw_scope or "")
            ).strip().lower()
            if raw_value == normalized:
                canonical_values[normalized] = route
            else:
                alias_values[normalized] = route
        workflows.update(alias_values)
        workflows.update(canonical_values)

    def string(key: str, fallback: str = "") -> str:
        value = migrated.get(key, fallback)
        return value.strip() if isinstance(value, str) else fallback

    transcription_provider = string("transcription_provider", "gemini").lower()
    if transcription_provider not in SUPPORTED_PROVIDERS:
        transcription_provider = "gemini"
    requested_refinement_provider = string("refinement_provider").lower()
    refinement_provider = requested_refinement_provider
    refinement_fallback = False
    if refinement_provider not in SUPPORTED_PROVIDERS:
        refinement_fallback = True
        refinement_provider = (
            transcription_provider
            if transcription_provider in ("openai", "groq")
            else "openai"
        )
    if not PROVIDER_REGISTRY.supports(
            refinement_provider, ProviderCapability.TEXT_GENERATION):
        refinement_fallback = True
        refinement_provider = (
            transcription_provider
            if (PROVIDER_REGISTRY.supports(
                    transcription_provider, ProviderCapability.TEXT_GENERATION)
                and not PROVIDER_REGISTRY.supports(
                    transcription_provider,
                    ProviderCapability.MULTIMODAL_AUDIO))
            else "openai"
        )
    defaults = {
        "gemini": ("gemini-2.5-flash", "gemini-2.5-flash"),
        "openai": ("whisper-1", "gpt-4o-mini"),
        "groq": ("whisper-large-v3-turbo", "llama-3.3-70b-versatile"),
        "local_asr": ("ggml-small", ""),
    }
    transcription_model = string(
        "gemini_model" if transcription_provider == "gemini"
        else f"{transcription_provider}_audio_model",
        defaults.get(transcription_provider, ("", ""))[0],
    )
    if transcription_provider == "gemini":
        transcription_model = transcription_model or defaults["gemini"][0]
    refinement_model = "" if refinement_fallback else string(
        "refinement_model",
        string(f"{refinement_provider}_text_model",
               defaults.get(refinement_provider, ("", ""))[1]),
    )
    local_enabled = migrated.get("local_asr_cloud_refinement", False)
    local_enabled = local_enabled if isinstance(local_enabled, bool) else False

    defaults_for_scope = {
        WorkflowScope.TRANSCRIPTION.value: {
            "provider_id": transcription_provider,
            "model_id": transcription_model,
            "prompt": DEFAULT_WORKFLOW_PROMPTS[WorkflowScope.TRANSCRIPTION.value],
            "custom_endpoint": "",
            "enabled": True,
        },
        WorkflowScope.REFINEMENT.value: {
            "provider_id": refinement_provider,
            "model_id": refinement_model,
            "prompt": DEFAULT_WORKFLOW_PROMPTS[WorkflowScope.REFINEMENT.value],
            "custom_endpoint": "",
            "enabled": True,
        },
        WorkflowScope.REWRITE.value: {
            "provider_id": refinement_provider,
            "model_id": refinement_model,
            "prompt": DEFAULT_WORKFLOW_PROMPTS[WorkflowScope.REWRITE.value],
            "custom_endpoint": "",
            "enabled": True,
        },
        WorkflowScope.TRANSLATION.value: {
            "provider_id": refinement_provider,
            "model_id": refinement_model,
            "prompt": DEFAULT_WORKFLOW_PROMPTS[WorkflowScope.TRANSLATION.value],
            "custom_endpoint": "",
            "enabled": True,
        },
        WorkflowScope.LOCAL_ASR_REFINEMENT.value: {
            "provider_id": refinement_provider,
            "model_id": refinement_model,
            "prompt": DEFAULT_WORKFLOW_PROMPTS[
                WorkflowScope.LOCAL_ASR_REFINEMENT.value
            ],
            "custom_endpoint": "",
            "enabled": local_enabled,
        },
    }
    for scope, default in defaults_for_scope.items():
        if not str(default.get("model_id", "")).strip():
            try:
                metadata = PROVIDER_REGISTRY.describe(default["provider_id"])
                default["model_id"] = (
                    metadata.default_audio_model
                    if scope == WorkflowScope.TRANSCRIPTION.value
                    else metadata.default_text_model
                )
            except (ProviderError, KeyError, ValueError):
                pass
        if not isinstance(workflows.get(scope), Mapping):
            workflows[scope] = default
        else:
            # Do not overwrite a partially authored future route.  Missing
            # fields receive deterministic defaults and the migration remains
            # idempotent when called again.
            route = dict(workflows[scope])
            # A v1 route may override the shared legacy provider while
            # omitting its model.  Canonicalize route aliases before filling
            # missing fields, then derive that model from the route provider;
            # otherwise a Groq route could inherit OpenAI's text model and
            # fail only when the workflow is first used.
            for canonical, aliases in (
                    ("provider_id", ("provider",)),
                    ("model_id", ("model",)),
                    ("custom_endpoint", ("endpoint", "base_url"))):
                if canonical not in route:
                    for alias in aliases:
                        if alias in route:
                            route[canonical] = route[alias]
                            break
            route_provider = route.get("provider_id", default["provider_id"])
            if not isinstance(route_provider, str) or not route_provider.strip():
                route_provider = default["provider_id"]
            route_provider = route_provider.strip().lower()
            route_model = str(route.get("model_id", "") or "").strip()
            if not route_model:
                try:
                    metadata = PROVIDER_REGISTRY.describe(route_provider)
                    route["model_id"] = (
                        metadata.default_audio_model
                        if scope == WorkflowScope.TRANSCRIPTION.value
                        else metadata.default_text_model
                    )
                except (ProviderError, KeyError, ValueError):
                    # Preserve the legacy fallback for unknown providers; the
                    # normal validation boundary will report that bad route.
                    pass
            for key, value in default.items():
                route.setdefault(key, value)
            workflows[scope] = route
    migrated["workflows"] = workflows
    migrated["schema_version"] = 2
    return migrated


CONFIG_MIGRATIONS = {0: _migrate_legacy_to_v1, 1: _migrate_v1_to_v2}


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

    def _model_from_mapping(
        self,
        config: Mapping[str, Any],
        current_payload: Mapping[str, Any],
        *,
        preserve_explicit_endpoints: bool = False,
        synchronize_legacy: bool = True,
    ) -> AppConfig:
        """Merge partial legacy input with disk before normalizing routes."""
        merged = dict(current_payload)
        incoming = dict(config)
        current_workflows = current_payload.get("workflows")
        incoming_workflows = incoming.get("workflows")
        merged.update(incoming)

        def split_workflow_values(values: Any):
            """Split aliases so canonical scope names have deterministic precedence."""
            unknown: dict[str, Any] = {}
            aliases: dict[str, Any] = {}
            canonical: dict[str, Any] = {}
            if not isinstance(values, Mapping):
                return unknown, aliases, canonical
            for raw_scope, route in values.items():
                scope = normalize_workflow_scope(raw_scope)
                if scope not in WORKFLOW_SCOPES:
                    unknown[scope] = route
                    continue
                raw_value = (
                    raw_scope.value
                    if isinstance(raw_scope, WorkflowScope)
                    else str(raw_scope or "")
                ).strip().lower()
                target = canonical if raw_value == scope else aliases
                target[scope] = route
            return unknown, aliases, canonical

        def canonicalize_route(route: Any):
            if not isinstance(route, Mapping):
                return route
            route = dict(route)
            for canonical, aliases in (
                    ("provider_id", ("provider",)),
                    ("model_id", ("model",)),
                    ("custom_endpoint", ("endpoint", "base_url"))):
                if canonical not in route:
                    for alias in aliases:
                        if alias in route:
                            route[canonical] = route[alias]
                            break
            return route

        current_unknown, current_aliases, current_canonical = (
            split_workflow_values(current_workflows))
        incoming_unknown, incoming_aliases, incoming_canonical = (
            split_workflow_values(incoming_workflows))
        current_route_values = dict(current_unknown)
        current_route_values.update(current_aliases)
        current_route_values.update(current_canonical)
        if isinstance(incoming_workflows, Mapping):
            workflow_values: dict[str, Any] = dict(current_route_values)
            # If a payload contains both spellings, the canonical route is
            # authoritative, matching WorkflowConfig.from_mapping() and
            # migrations. Each selected route still deep-merges with the
            # persisted route so partial updates retain omitted fields.
            incoming_route_values = dict(incoming_unknown)
            incoming_route_values.update(incoming_aliases)
            incoming_route_values.update(incoming_canonical)
            for scope, route in incoming_route_values.items():
                route = canonicalize_route(route)
                previous_route = workflow_values.get(scope)
                if isinstance(previous_route, Mapping) and isinstance(route, Mapping):
                    merged_route = dict(previous_route)
                    merged_route.update(dict(route))
                    workflow_values[scope] = merged_route
                else:
                    workflow_values[scope] = route
            merged["workflows"] = workflow_values
        model = AppConfig.from_mapping(merged, self.defaults)
        legacy_workflow_keys = {
            "transcription_provider",
            "refinement_provider",
            "refinement_model",
            "local_asr_cloud_refinement",
        }
        legacy_workflow_keys.update(
            metadata.audio_model_key for metadata in PROVIDER_REGISTRY.metadata
        )
        changed_keys = {
            key for key in legacy_workflow_keys
            if key in config and config.get(key) != current_payload.get(key)
        }
        explicit_endpoint_scopes: frozenset[str] = frozenset()
        if preserve_explicit_endpoints and isinstance(incoming_workflows, Mapping):
            explicit_endpoint_scopes = frozenset(
                scope
                for scope, route in {
                    **incoming_aliases, **incoming_canonical
                }.items()
                if isinstance(route, Mapping)
                and any(
                    key in route
                    for key in ("custom_endpoint", "endpoint", "base_url")
                )
                and self._mapping_route_explicitly_changed(
                    scope, route, current_route_values)
            )
        if changed_keys and synchronize_legacy:
            legacy_values = dict(current_payload)
            legacy_values.update(dict(config))
            legacy_values.pop("workflows", None)
            legacy_model = AppConfig.from_mapping(legacy_values, self.defaults)
            model = model.synchronize_legacy_routes(
                legacy_model,
                changed_keys,
                preserve_endpoint_scopes=explicit_endpoint_scopes,
            )
        return model

    @staticmethod
    def _mapping_route_explicitly_changed(
        scope: str,
        route: Mapping[str, Any],
        current_workflows: Any,
    ) -> bool:
        """Detect an intentional nested route edit in a compatibility mapping."""
        if not isinstance(current_workflows, Mapping):
            return True
        previous = current_workflows.get(scope)
        if not isinstance(previous, Mapping):
            return True

        def value(values: Mapping[str, Any], *keys: str) -> str:
            for key in keys:
                if key in values and isinstance(values[key], str):
                    return values[key].strip()
            return ""

        incoming_endpoint = value(route, "custom_endpoint", "endpoint", "base_url")
        previous_endpoint = value(
            previous, "custom_endpoint", "endpoint", "base_url")
        if incoming_endpoint.rstrip("/") != previous_endpoint.rstrip("/"):
            return True
        return any(
            key in route
            and value(route, key) != value(previous, key)
            for key in ("provider_id", "provider", "model_id", "model")
        )

    def save(
        self,
        config: AppConfig | Mapping[str, Any],
        *,
        _synchronize_legacy: bool = True,
    ) -> None:
        with self._lock:
            _ensure_supported_schema(self.path, CONFIG_SCHEMA_VERSION)
            current_payload = migrate_config_payload(
                _read_json_mapping(self.path) or {}
            )
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
                model = self._model_from_mapping(
                    config,
                    current_payload,
                    synchronize_legacy=_synchronize_legacy,
                )
                supplied_keys = set(config)

            # ``save`` is also a public application-facing write path (the
            # legacy UI still uses it), so it must enforce the same route
            # boundary as ``apply`` before serializing any endpoint or model.
            model = replace(
                model,
                workflows=validate_workflow_config(model.workflows),
            )
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

    def apply(self, config: AppConfig | Mapping[str, Any]) -> AppConfig:
        """Validate routes, then persist config as one rollback-capable unit.

        ``save`` already coordinates secret-store verification and the atomic
        JSON replacement.  Keeping this higher-level operation separate lets
        settings callers validate every workflow before touching a provider,
        sidecar, or credential backend.
        """
        with self._lock:
            if isinstance(config, AppConfig):
                model = config
                supplied_keys = None
            else:
                _ensure_supported_schema(self.path, CONFIG_SCHEMA_VERSION)
                supplied_version = _version(config.get("schema_version"))
                if supplied_version > CONFIG_SCHEMA_VERSION:
                    raise UnsupportedSchemaVersionError(
                        f"Cannot save schema version {supplied_version} "
                        f"with supported version {CONFIG_SCHEMA_VERSION}")
                current_payload = migrate_config_payload(
                    _read_json_mapping(self.path) or {}
                )
                model = self._model_from_mapping(
                    config,
                    current_payload,
                    preserve_explicit_endpoints=True,
                )
                supplied_keys = set(config)
            validated = replace(
                model,
                workflows=validate_workflow_config(model.workflows),
            )
            if supplied_keys is None:
                self.save(validated)
                return self.load()
            persisted = validated.to_mapping()
            for key in PROVIDER_SECRET_KEYS.values():
                if key not in supplied_keys:
                    persisted.pop(key, None)
            self.save(persisted, _synchronize_legacy=False)
            return self.load()

    def reset_workflow(self, scope: WorkflowScope | str) -> AppConfig:
        """Reset and persist one workflow route while retaining other settings."""
        current = self.load()
        return self.apply(current.reset_workflow(scope))

    def test_workflow(
        self,
        scope: WorkflowScope | str | None = None,
    ) -> WorkflowTestResult | tuple[WorkflowTestResult, ...]:
        """Return a safe local route check without making network requests."""
        return test_workflow_configuration(self.load().workflows, scope)


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
