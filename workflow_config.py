"""Typed, UI-free provider routing for each ClarifyVoice workflow.

This module deliberately has no persistence or desktop imports.  It validates
provider capabilities and normalizes route IDs before a caller starts a
network request or local sidecar.  :mod:`repositories` owns serialization and
keeps these public types available as compatibility imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from provider_registry import PROVIDER_REGISTRY
from provider_types import ProviderCapability, ProviderError


class WorkflowScope(str, Enum):
    """Stable routing scopes persisted independently in ``config.json``."""

    TRANSCRIPTION = "transcription"
    REFINEMENT = "refinement"
    REWRITE = "rewrite"
    TRANSLATION = "translation"
    LOCAL_ASR_REFINEMENT = "local_asr_refinement"


WORKFLOW_SCOPES = tuple(scope.value for scope in WorkflowScope)
_WORKFLOW_SCOPE_ALIASES = {
    "dictation": WorkflowScope.TRANSCRIPTION.value,
    "cleanup": WorkflowScope.REFINEMENT.value,
    "text_refinement": WorkflowScope.REFINEMENT.value,
    "local-refinement": WorkflowScope.LOCAL_ASR_REFINEMENT.value,
}
WORKFLOW_CAPABILITIES = {
    WorkflowScope.TRANSCRIPTION.value: ProviderCapability.AUDIO_TRANSCRIPTION,
    WorkflowScope.REFINEMENT.value: ProviderCapability.TEXT_GENERATION,
    WorkflowScope.REWRITE.value: ProviderCapability.TEXT_GENERATION,
    WorkflowScope.TRANSLATION.value: ProviderCapability.TEXT_GENERATION,
    WorkflowScope.LOCAL_ASR_REFINEMENT.value: ProviderCapability.TEXT_GENERATION,
}
DEFAULT_WORKFLOW_PROMPTS = {
    WorkflowScope.TRANSCRIPTION.value: "Transcribe this audio.",
    WorkflowScope.REFINEMENT.value: (
        "Rewrite the transcript clearly while preserving its meaning."
    ),
    WorkflowScope.REWRITE.value: (
        "Rewrite the selected text clearly while preserving every requirement."
    ),
    WorkflowScope.TRANSLATION.value: (
        "Translate the selected text literally without adding commentary."
    ),
    WorkflowScope.LOCAL_ASR_REFINEMENT.value: (
        "Rewrite the local transcript clearly while preserving its meaning."
    ),
}
_SENSITIVE_ENDPOINT_QUERY_KEYS = frozenset({
    "api-key", "api_key", "apikey", "access_token", "auth", "authorization",
    "client_secret", "credential", "password", "passwd", "secret",
    "key", "sig", "signature", "token",
})


def normalize_workflow_scope(scope: WorkflowScope | str) -> str:
    """Normalize a public scope or legacy alias to its persisted value."""
    value = scope.value if isinstance(scope, WorkflowScope) else str(scope or "")
    value = value.strip().lower()
    return _WORKFLOW_SCOPE_ALIASES.get(value, value)


def _safe_endpoint_for_diagnostics(endpoint: str) -> str:
    """Keep only public URL components in diagnostics, never URL secrets."""
    value = str(endpoint or "").strip().rstrip("/")
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        if parts.scheme.lower() not in ("http", "https") or not hostname:
            return ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = parts.port
        netloc = hostname if port is None else f"{hostname}:{port}"
        return urlunsplit((parts.scheme.lower(), netloc, parts.path, "", ""))
    except ValueError:
        return ""


class WorkflowConfigurationError(ValueError):
    """A route cannot run with the selected provider capabilities."""

    def __init__(
        self,
        scope: WorkflowScope | str,
        message: str,
        *,
        provider_id: str = "",
        capability: ProviderCapability | None = None,
        field: str = "",
    ) -> None:
        self.scope = normalize_workflow_scope(scope)
        self.provider_id = str(provider_id or "").strip().lower()
        self.capability = capability
        self.field = str(field or "")
        super().__init__(message)


@dataclass(frozen=True)
class WorkflowRoute:
    """One UI-independent provider route and its prompt policy."""

    provider_id: str = "openai"
    model_id: str = "gpt-4o-mini"
    prompt: str = ""
    custom_endpoint: str = ""
    enabled: bool = True

    @property
    def provider(self) -> str:
        return self.provider_id

    @property
    def model(self) -> str:
        return self.model_id

    @property
    def endpoint(self) -> str:
        return self.custom_endpoint

    @property
    def base_url(self) -> str:
        return self.custom_endpoint

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any] | None,
        *,
        default: "WorkflowRoute | None" = None,
    ) -> "WorkflowRoute":
        fallback = default or cls()
        if not isinstance(values, Mapping):
            return fallback

        def text(*keys: str, fallback_value: str = "") -> str:
            for key in keys:
                value = values.get(key)
                if isinstance(value, str):
                    return value.strip()
            return fallback_value

        enabled = values.get("enabled", fallback.enabled)
        if not isinstance(enabled, bool):
            enabled = fallback.enabled
        return cls(
            provider_id=(text(
                "provider_id", "provider", fallback_value=fallback.provider_id
            ).lower() or fallback.provider_id),
            model_id=text("model_id", "model", fallback_value=fallback.model_id)
            or fallback.model_id,
            prompt=text("prompt", fallback_value=fallback.prompt),
            custom_endpoint=text(
                "custom_endpoint", "endpoint", "base_url",
                fallback_value=fallback.custom_endpoint,
            ),
            enabled=enabled,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "prompt": self.prompt,
            "custom_endpoint": self.custom_endpoint,
            "enabled": self.enabled,
        }

    def without_prompt(self) -> dict[str, Any]:
        """Safe route summary for diagnostics and usage metadata."""
        values = self.to_mapping()
        values.pop("prompt", None)
        values["custom_endpoint"] = _safe_endpoint_for_diagnostics(
            self.custom_endpoint)
        return values


@dataclass(frozen=True)
class WorkflowConfig(Mapping[str, WorkflowRoute]):
    """Typed routes for transcription, refinement, rewrite and translation."""

    transcription: WorkflowRoute = field(default_factory=lambda: WorkflowRoute(
        provider_id="gemini", model_id="gemini-2.5-flash",
        prompt=DEFAULT_WORKFLOW_PROMPTS[WorkflowScope.TRANSCRIPTION.value],
    ))
    refinement: WorkflowRoute = field(default_factory=lambda: WorkflowRoute(
        prompt=DEFAULT_WORKFLOW_PROMPTS[WorkflowScope.REFINEMENT.value],
    ))
    rewrite: WorkflowRoute = field(default_factory=lambda: WorkflowRoute(
        prompt=DEFAULT_WORKFLOW_PROMPTS[WorkflowScope.REWRITE.value],
    ))
    translation: WorkflowRoute = field(default_factory=lambda: WorkflowRoute(
        prompt=DEFAULT_WORKFLOW_PROMPTS[WorkflowScope.TRANSLATION.value],
    ))
    local_asr_refinement: WorkflowRoute = field(
        default_factory=lambda: WorkflowRoute(
            prompt=DEFAULT_WORKFLOW_PROMPTS[
                WorkflowScope.LOCAL_ASR_REFINEMENT.value
            ],
            enabled=False,
        )
    )

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any] | None,
        *,
        defaults: "WorkflowConfig | None" = None,
    ) -> "WorkflowConfig":
        fallback = defaults or cls()
        if not isinstance(values, Mapping):
            return fallback
        routes = {
            scope: WorkflowRoute.from_mapping(
                values.get(scope), default=fallback[scope]
            )
            for scope in WORKFLOW_SCOPES
        }
        if "refinement" not in values and "cleanup" in values:
            routes[WorkflowScope.REFINEMENT.value] = WorkflowRoute.from_mapping(
                values.get("cleanup"), default=fallback.refinement
            )
        return cls(**routes)

    def __getitem__(self, scope: str) -> WorkflowRoute:
        normalized = normalize_workflow_scope(scope)
        if normalized not in WORKFLOW_SCOPES:
            raise KeyError(scope)
        return getattr(self, normalized)

    def __iter__(self):
        return iter(WORKFLOW_SCOPES)

    def __len__(self) -> int:
        return len(WORKFLOW_SCOPES)

    def route(self, scope: WorkflowScope | str) -> WorkflowRoute:
        return self[normalize_workflow_scope(scope)]

    def with_route(
        self, scope: WorkflowScope | str, route: WorkflowRoute
    ) -> "WorkflowConfig":
        normalized = normalize_workflow_scope(scope)
        if normalized not in WORKFLOW_SCOPES:
            raise KeyError(scope)
        return replace(self, **{normalized: route})

    def to_mapping(self) -> dict[str, dict[str, Any]]:
        return {scope: self[scope].to_mapping() for scope in WORKFLOW_SCOPES}

    def diagnostic_mapping(self) -> dict[str, dict[str, Any]]:
        return {scope: self[scope].without_prompt() for scope in WORKFLOW_SCOPES}


@dataclass(frozen=True)
class WorkflowTestResult:
    """Result of a local capability/endpoint check, with no network call."""

    scope: str
    provider_id: str
    model_id: str
    capability: str
    endpoint: str
    enabled: bool
    ok: bool = True

    def to_mapping(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "capability": self.capability,
            "endpoint": self.endpoint,
            "enabled": self.enabled,
            "ok": self.ok,
        }


def _default_model_for_scope(metadata, scope: str) -> str:
    capability = WORKFLOW_CAPABILITIES[scope]
    if capability is ProviderCapability.AUDIO_TRANSCRIPTION:
        return str(metadata.default_audio_model or "").strip()
    return str(metadata.default_text_model or "").strip()


def validate_workflow_route(
    route: WorkflowRoute,
    scope: WorkflowScope | str,
    *,
    registry=PROVIDER_REGISTRY,
) -> WorkflowRoute:
    """Validate and canonicalize one route before provider work starts."""
    normalized_scope = normalize_workflow_scope(scope)
    capability = WORKFLOW_CAPABILITIES.get(normalized_scope)
    if capability is None:
        raise WorkflowConfigurationError(
            normalized_scope, f"Unknown workflow scope: {normalized_scope}",
            field="scope",
        )
    if not isinstance(route, WorkflowRoute):
        route = WorkflowRoute.from_mapping(route if isinstance(route, Mapping) else None)
    provider = str(route.provider_id or "").strip().lower()
    if not provider:
        raise WorkflowConfigurationError(
            normalized_scope, "A provider is required for this workflow.",
            capability=capability, field="provider_id",
        )
    try:
        metadata = registry.describe(provider)
    except (ProviderError, KeyError, ValueError) as error:
        raise WorkflowConfigurationError(
            normalized_scope,
            f"Unknown provider '{provider}'. Choose a registered provider.",
            provider_id=provider, capability=capability, field="provider_id",
        ) from error
    if not metadata.supports(capability):
        raise WorkflowConfigurationError(
            normalized_scope,
            f"{metadata.display_name} does not support "
            f"{capability.value.replace('_', ' ')} for this workflow.",
            provider_id=provider, capability=capability, field="provider_id",
        )
    model = str(route.model_id or "").strip()
    if not model:
        model = _default_model_for_scope(metadata, normalized_scope)
    if not model:
        raise WorkflowConfigurationError(
            normalized_scope,
            f"{metadata.display_name} has no default model for this workflow.",
            provider_id=provider, capability=capability, field="model_id",
        )
    if capability is ProviderCapability.AUDIO_TRANSCRIPTION:
        try:
            model = registry.canonical_audio_model(provider, model)
        except (ProviderError, ValueError) as error:
            raise WorkflowConfigurationError(
                normalized_scope, "The audio model is not valid for this provider.",
                provider_id=provider, capability=capability, field="model_id",
            ) from error
    endpoint = str(route.custom_endpoint or "").strip().rstrip("/")
    if endpoint:
        if not metadata.supports(ProviderCapability.CUSTOM_BASE_URL):
            raise WorkflowConfigurationError(
                normalized_scope,
                f"{metadata.display_name} does not support custom endpoints.",
                provider_id=provider,
                capability=ProviderCapability.CUSTOM_BASE_URL,
                field="custom_endpoint",
            )
        if not endpoint.startswith(("https://", "http://")):
            raise WorkflowConfigurationError(
                normalized_scope, "Custom endpoint must be an HTTP(S) URL.",
                provider_id=provider, field="custom_endpoint",
            )
        try:
            endpoint_parts = urlsplit(endpoint)
            query_keys = {
                key.strip().lower()
                for key, _value in parse_qsl(
                        endpoint_parts.query, keep_blank_values=True)
            }
            has_sensitive_query = bool(
                query_keys & _SENSITIVE_ENDPOINT_QUERY_KEYS)
            if (not endpoint_parts.hostname or endpoint_parts.username
                    or endpoint_parts.password or has_sensitive_query):
                raise WorkflowConfigurationError(
                    normalized_scope,
                    "Custom endpoint must not contain URL credentials or tokens.",
                    provider_id=provider, field="custom_endpoint",
                )
        except ValueError as error:
            raise WorkflowConfigurationError(
                normalized_scope, "Custom endpoint must be an HTTP(S) URL.",
                provider_id=provider, field="custom_endpoint",
            ) from error
    prompt = str(route.prompt or "").strip()
    if not prompt:
        prompt = DEFAULT_WORKFLOW_PROMPTS[normalized_scope]
    return WorkflowRoute(
        provider_id=provider,
        model_id=model,
        prompt=prompt,
        custom_endpoint=endpoint,
        enabled=bool(route.enabled),
    )


def validate_workflow_config(
    workflows: WorkflowConfig | Mapping[str, Any], *, registry=PROVIDER_REGISTRY
) -> WorkflowConfig:
    """Validate all scopes with one deterministic, side-effect-free pass."""
    if not isinstance(workflows, WorkflowConfig):
        workflows = WorkflowConfig.from_mapping(workflows)
    validated = workflows
    for scope in WORKFLOW_SCOPES:
        validated = validated.with_route(
            scope, validate_workflow_route(validated[scope], scope, registry=registry)
        )
    return validated


def test_workflow_configuration(
    workflows: WorkflowConfig | Mapping[str, Any],
    scope: WorkflowScope | str | None = None,
    *,
    registry=PROVIDER_REGISTRY,
) -> WorkflowTestResult | tuple[WorkflowTestResult, ...]:
    """Run a safe local configuration test; network and sidecars are untouched."""
    if scope is not None and normalize_workflow_scope(scope) not in WORKFLOW_SCOPES:
        normalized = normalize_workflow_scope(scope)
        raise WorkflowConfigurationError(
            normalized, f"Unknown workflow scope: {normalized}", field="scope"
        )
    normalized_scope = normalize_workflow_scope(scope) if scope is not None else None
    if not isinstance(workflows, WorkflowConfig):
        workflows = WorkflowConfig.from_mapping(workflows)
    if normalized_scope is not None:
        route = validate_workflow_route(
            workflows[normalized_scope], normalized_scope, registry=registry)
        validated = workflows.with_route(normalized_scope, route)
        scopes = (normalized_scope,)
    else:
        validated = validate_workflow_config(workflows, registry=registry)
        scopes = WORKFLOW_SCOPES
    results = tuple(
        WorkflowTestResult(
            scope=current,
            provider_id=validated[current].provider_id,
            model_id=validated[current].model_id,
            capability=WORKFLOW_CAPABILITIES[current].value,
            endpoint=_safe_endpoint_for_diagnostics(
                validated[current].custom_endpoint
                or str(registry.describe(
                    validated[current].provider_id).default_base_url)
            ),
            enabled=validated[current].enabled,
        )
        for current in scopes
    )
    return results[0] if scope is not None else results
