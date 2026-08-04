"""Typed contracts shared by ClarifyVoice provider adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol


MAX_TRANSCRIPTION_CONTEXT_CHARS = 4096


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
    # Optional provider-neutral vocabulary context.  Adapters that support
    # transcription prompts may forward it; local/offline adapters can ignore
    # it without adding a provider-specific workflow branch.
    dictionary_context: str = ""

    def effective_prompt(self) -> str:
        """Return the prompt with optional local vocabulary context appended."""
        prompt = str(self.prompt or "").strip()
        context = str(self.dictionary_context or "").strip()
        if len(context) > MAX_TRANSCRIPTION_CONTEXT_CHARS:
            raise ValueError(
                "dictionary transcription context exceeds the 4096-character limit")
        if not context:
            return prompt
        if not prompt:
            return context
        return f"{prompt}\n\n{context}"


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    provider_id: str
    model: str
    # Optional desktop-runtime provenance. Provider adapters that only return
    # one transcription leave these unset; the prompt-mode adapter fills them
    # when a separate refinement route transforms the raw transcript.
    raw_text: str | None = None
    refined_text: str | None = None
    refinement_provider_id: str | None = None
    refinement_model: str | None = None


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
    """Shared transport seam implemented by the provider HTTP policy."""

    def request(self, method: str, url: str, *, provider: str,
            operation: str, cancel_token: Any = None,
            safe_to_retry: bool | None = None, **kwargs: Any) -> HttpResponse: ...

    def json(self, response: HttpResponse, *, provider: str,
            operation: str) -> Any: ...
