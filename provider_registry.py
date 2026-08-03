"""Authoritative provider registry and legacy configuration adapter."""

from __future__ import annotations

from collections.abc import Mapping
import os
import platform
from pathlib import Path

from provider_adapters import GeminiAdapter, OpenAICompatibleAdapter, ProviderAdapter
from provider_http import ProviderHttpClient, SafeRotatingLogger
from provider_types import (
    HttpClient,
    ModelCatalog,
    ProviderCapability,
    ProviderConnection,
    ProviderMetadata,
    RewriteRequest,
    RewriteResult,
    TranscriptionRequest,
    TranscriptionResult,
    TranslationRequest,
    TranslationResult,
    UnknownProviderError,
    UnsupportedCapabilityError,
)
from local_asr import LocalASRProviderAdapter


OPENAI_OFFICIAL_AUDIO_MODELS = (
    "whisper-1",
    "gpt-4o-mini-transcribe",
    "gpt-4o-transcribe",
    "gpt-4o-transcribe-diarize",
)
GROQ_OFFICIAL_AUDIO_MODELS = (
    "whisper-large-v3-turbo",
    "whisper-large-v3",
)


class ProviderRegistry:
    """Routes typed requests by capability instead of workflow name branches."""

    def __init__(self):
        self._adapters: dict[str, ProviderAdapter] = {}

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(self._adapters)

    @property
    def metadata(self) -> tuple[ProviderMetadata, ...]:
        return tuple(adapter.metadata for adapter in self._adapters.values())

    def register(self, adapter: ProviderAdapter) -> None:
        provider_id = adapter.metadata.provider_id.strip().lower()
        if not provider_id:
            raise ValueError("provider ID is required")
        if provider_id in self._adapters:
            raise ValueError(f"provider '{provider_id}' is already registered")
        self._adapters[provider_id] = adapter

    def register_openai_compatible(self, metadata: ProviderMetadata,
            http: HttpClient | None = None, official_host: str = "",
            official_audio_models: tuple[str, ...] = (),
            audio_model_aliases: Mapping[str, str] | None = None) -> None:
        """Register another compatible provider without changing workflows."""
        self.register(OpenAICompatibleAdapter(
            metadata, http or ProviderHttpClient(),
            official_host=official_host,
            official_audio_models=official_audio_models,
            audio_model_aliases=audio_model_aliases,
        ))

    def adapter(self, provider_id: str,
            capability: ProviderCapability | None = None):
        normalized = str(provider_id or "").strip().lower()
        adapter = self._adapters.get(normalized)
        if adapter is None:
            choices = ", ".join(self.provider_ids)
            raise UnknownProviderError(
                normalized,
                f"Unknown provider '{normalized}'. Choose one of: {choices}.",
                capability,
            )
        if capability is not None and not adapter.metadata.supports(capability):
            raise UnsupportedCapabilityError(
                normalized,
                f"{adapter.metadata.display_name} does not support "
                f"{capability.value.replace('_', ' ')}. Choose a provider that does.",
                capability,
            )
        return adapter

    def describe(self, provider_id: str) -> ProviderMetadata:
        return self.adapter(provider_id).metadata

    def supports(self, provider_id: str, capability: ProviderCapability) -> bool:
        try:
            return self.describe(provider_id).supports(capability)
        except UnknownProviderError:
            return False

    def connection_from_legacy(self, provider_id: str,
            config: Mapping[str, object]) -> ProviderConnection:
        metadata = self.describe(provider_id)
        return ProviderConnection(
            str(config.get(f"{metadata.provider_id}_api_key", "")).strip(),
            str(config.get(
                f"{metadata.provider_id}_base_url",
                metadata.default_base_url,
            )).strip(),
        )

    def audio_model_from_legacy(self, provider_id: str,
            config: Mapping[str, object]) -> str:
        metadata = self.describe(provider_id)
        model = str(config.get(
            metadata.audio_model_key, metadata.default_audio_model)).strip()
        return self.canonical_audio_model(provider_id, model)

    def text_model_from_legacy(self, provider_id: str,
            config: Mapping[str, object], override: str = "") -> str:
        metadata = self.describe(provider_id)
        return (str(override or "").strip() or str(config.get(
            metadata.text_model_key, metadata.default_text_model)).strip()
            or metadata.default_text_model)

    def canonical_audio_model(self, provider_id: str, model: str) -> str:
        adapter = self.adapter(provider_id)
        canonicalize = getattr(adapter, "canonical_audio_model", None)
        return canonicalize(model) if canonicalize else str(model or "").strip()

    def parse_audio_models(self, provider_id: str,
            payload: Mapping[str, object]) -> tuple[str, ...]:
        return self.adapter(
            provider_id, ProviderCapability.MODEL_DISCOVERY,
        ).parse_audio_models(payload)

    def parse_text_models(self, provider_id: str,
            payload: Mapping[str, object]) -> tuple[str, ...]:
        return self.adapter(
            provider_id, ProviderCapability.MODEL_DISCOVERY,
        ).parse_text_models(payload)

    def validate(self, provider_id: str,
            connection: ProviderConnection,
            cancel_token=None) -> Mapping[str, object]:
        return self.adapter(
            provider_id, ProviderCapability.MODEL_DISCOVERY,
        ).validate(connection, cancel_token)

    def discover_models(self, provider_id: str,
            connection: ProviderConnection, cancel_token=None) -> ModelCatalog:
        return self.adapter(
            provider_id, ProviderCapability.MODEL_DISCOVERY,
        ).discover_models(connection, cancel_token)

    def fetch_audio_models(self, provider_id: str,
            connection: ProviderConnection, cancel_token=None) -> tuple[str, ...]:
        return self.adapter(
            provider_id, ProviderCapability.MODEL_DISCOVERY,
        ).fetch_audio_models(connection, cancel_token)

    def transcribe(self, provider_id: str, request: TranscriptionRequest,
            connection: ProviderConnection, cancel_token=None) -> TranscriptionResult:
        return self.adapter(
            provider_id, ProviderCapability.AUDIO_TRANSCRIPTION,
        ).transcribe(request, connection, cancel_token)

    def rewrite(self, provider_id: str, request: RewriteRequest,
            connection: ProviderConnection, cancel_token=None) -> RewriteResult:
        return self.adapter(
            provider_id, ProviderCapability.TEXT_GENERATION,
        ).rewrite(request, connection, cancel_token)

    def translate(self, provider_id: str, request: TranslationRequest,
            connection: ProviderConnection, cancel_token=None) -> TranslationResult:
        return self.adapter(
            provider_id, ProviderCapability.TEXT_GENERATION,
        ).translate(request, connection, cancel_token)

    def cancel(self) -> None:
        """Cancel in-flight work owned by adapters that expose lifecycle hooks."""
        for adapter in tuple(self._adapters.values()):
            cancel = getattr(adapter, "cancel", None)
            if callable(cancel):
                cancel()

    def shutdown(self) -> None:
        """Release adapter-owned workers/processes during application exit."""
        for adapter in tuple(self._adapters.values()):
            shutdown = getattr(adapter, "shutdown", None)
            if callable(shutdown):
                shutdown()


def build_provider_registry(
        http: HttpClient | None = None,
        *,
        local_asr_adapter: LocalASRProviderAdapter | None = None,
) -> ProviderRegistry:
    http = http or ProviderHttpClient()
    registry = ProviderRegistry()
    shared = frozenset({
        ProviderCapability.AUDIO_TRANSCRIPTION,
        ProviderCapability.TEXT_GENERATION,
        ProviderCapability.MODEL_DISCOVERY,
        ProviderCapability.CUSTOM_BASE_URL,
    })
    registry.register(GeminiAdapter(ProviderMetadata(
        provider_id="gemini",
        display_name="Gemini",
        capabilities=shared | {ProviderCapability.MULTIMODAL_AUDIO},
        default_base_url="https://generativelanguage.googleapis.com/v1beta",
        audio_model_key="gemini_model",
        text_model_key="gemini_model",
        default_audio_model="gemini-2.5-flash",
        default_text_model="gemini-2.5-flash",
    ), http))
    registry.register_openai_compatible(ProviderMetadata(
        provider_id="openai",
        display_name="OpenAI",
        capabilities=shared,
        default_base_url="https://api.openai.com/v1",
        audio_model_key="openai_audio_model",
        text_model_key="openai_text_model",
        default_audio_model="whisper-1",
        default_text_model="gpt-4o-mini",
    ), http, official_host="api.openai.com",
        official_audio_models=OPENAI_OFFICIAL_AUDIO_MODELS,
        audio_model_aliases={"Whisper 1": "whisper-1"})
    registry.register_openai_compatible(ProviderMetadata(
        provider_id="groq",
        display_name="Groq",
        capabilities=shared,
        default_base_url="https://api.groq.com/openai/v1",
        audio_model_key="groq_audio_model",
        text_model_key="groq_text_model",
        default_audio_model="whisper-large-v3-turbo",
        default_text_model="llama-3.3-70b-versatile",
    ), http, official_host="api.groq.com",
        official_audio_models=GROQ_OFFICIAL_AUDIO_MODELS,
        audio_model_aliases={
            "Whisper Large V3 Turbo": "whisper-large-v3-turbo",
            "Whisper Large V3": "whisper-large-v3",
        })
    # The adapter is lightweight and performs no download/process work at
    # registration time.  Installation remains an explicit settings action;
    # selecting this provider before installation fails with an actionable
    # typed configuration error rather than falling back to cloud.
    registry.register(local_asr_adapter or LocalASRProviderAdapter())
    return registry


def _provider_data_directory() -> Path:
    if platform.system() == "Windows":
        return Path(os.environ.get("APPDATA", Path.home())) / "ClarifyVoice"
    return Path.home() / ".clarifyvoice"


PROVIDER_HTTP = ProviderHttpClient(
    logger=SafeRotatingLogger(_provider_data_directory() / "logs"))
PROVIDER_REGISTRY = build_provider_registry(PROVIDER_HTTP)
PROVIDER_IDS = PROVIDER_REGISTRY.provider_ids
