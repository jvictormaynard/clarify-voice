"""Typed contracts shared by ClarifyVoice provider adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol


class ProviderCapability(str, Enum):
    """Operations that a provider adapter can explicitly advertise."""

    AUDIO_TRANSCRIPTION = "audio_transcription"
    TEXT_GENERATION = "text_generation"
    MULTIMODAL_AUDIO = "multimodal_audio"
    MODEL_DISCOVERY = "model_discovery"
    CUSTOM_BASE_URL = "custom_base_url"


class ProviderError(RuntimeError):
    """Base error carrying provider and capability context for the caller."""

    def __init__(self, provider_id: str, message: str,
            capability: ProviderCapability | None = None):
        super().__init__(message)
        self.provider_id = provider_id
        self.capability = capability


class UnknownProviderError(ProviderError):
    """Raised when a provider ID is not registered."""


class UnsupportedCapabilityError(ProviderError):
    """Raised when a workflow requests an operation the provider cannot do."""


class ProviderConfigurationError(ProviderError):
    """Raised when a provider cannot run with the supplied local settings."""


class ProviderResponseError(ProviderError):
    """Raised when a provider response does not satisfy its adapter contract."""


@dataclass(frozen=True)
class ProviderMetadata:
    """Stable registry metadata; API IDs are never display labels."""

    provider_id: str
    display_name: str
    capabilities: frozenset[ProviderCapability]
    default_base_url: str
    audio_model_key: str
    text_model_key: str
    default_audio_model: str
    default_text_model: str

    def supports(self, capability: ProviderCapability) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True)
class ProviderConnection:
    api_key: str
    base_url: str


@dataclass(frozen=True)
class ModelCatalog:
    audio_models: tuple[str, ...] = ()
    text_models: tuple[str, ...] = ()


@dataclass(frozen=True)
class TranscriptionRequest:
    audio_path: Path
    model: str
    language: str
    instruction: str
    prompt: str
    temperature: float
    # Optional in-memory snapshot used to decouple cleanup from long uploads.
    audio_bytes: bytes | None = None


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    provider_id: str
    model: str


@dataclass(frozen=True)
class RewriteRequest:
    text: str
    model: str
    language: str
    instruction: str
    source_message: str
    temperature: float = 0.1


@dataclass(frozen=True)
class RewriteResult:
    text: str
    provider_id: str
    model: str


@dataclass(frozen=True)
class TranslationRequest:
    text: str
    model: str
    target_language: str
    instruction: str
    source_message: str
    temperature: float = 0.0


@dataclass(frozen=True)
class TranslationResult:
    text: str
    provider_id: str
    model: str
    target_language: str


class HttpResponse(Protocol):
    text: str
    status_code: int

    def json(self) -> Mapping[str, Any]: ...

    def raise_for_status(self) -> None: ...


class HttpClient(Protocol):
    """Minimal transport seam; retry/session policy belongs to issue #17."""

    def get(self, url: str, **kwargs: Any) -> HttpResponse: ...

    def post(self, url: str, **kwargs: Any) -> HttpResponse: ...
