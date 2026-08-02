"""Provider adapters with no dependency on the desktop UI."""

from __future__ import annotations

import base64
import io
import mimetypes
from pathlib import Path
from typing import Any, Mapping

from provider_types import (
    HttpClient,
    ModelCatalog,
    ProviderCapability,
    ProviderConfigurationError,
    ProviderConnection,
    ProviderMetadata,
    ProviderResponseError,
    RewriteRequest,
    RewriteResult,
    TranscriptionRequest,
    TranscriptionResult,
    TranslationRequest,
    TranslationResult,
    UnsupportedCapabilityError,
)


def normalize_provider_url(base_url: str, version: str, endpoint: str) -> str:
    """Join a root or already-versioned base URL to a provider endpoint."""
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.lower().endswith(f"/{version.lower()}"):
        return f"{base}/{endpoint.lstrip('/')}"
    return f"{base}/{version}/{endpoint.lstrip('/')}"


def audio_mime_type(audio_path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(audio_path))
    return guessed if guessed and guessed.startswith("audio/") else "audio/wav"


def _model_entries(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    entries = payload.get("models", payload.get("data", []))
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, Mapping)]


class ProviderAdapter:
    """Common capability checks and catalog contract for concrete adapters."""

    def __init__(self, metadata: ProviderMetadata, http: HttpClient):
        self.metadata = metadata
        self.http = http

    def require(self, capability: ProviderCapability) -> None:
        if not self.metadata.supports(capability):
            raise UnsupportedCapabilityError(
                self.metadata.provider_id,
                f"{self.metadata.display_name} does not support "
                f"{capability.value.replace('_', ' ')}. Choose a provider that does.",
                capability,
            )

    def _require_connection(self, connection: ProviderConnection) -> None:
        if not connection.api_key.strip():
            raise ProviderConfigurationError(
                self.metadata.provider_id,
                f"No {self.metadata.display_name} API key",
            )
        if not connection.base_url.strip():
            raise ProviderConfigurationError(
                self.metadata.provider_id,
                f"No {self.metadata.display_name} base URL",
            )

    def parse_audio_models(self, payload: Mapping[str, Any]) -> tuple[str, ...]:
        raise NotImplementedError

    def parse_text_models(self, payload: Mapping[str, Any]) -> tuple[str, ...]:
        raise NotImplementedError

    def validate(self, connection: ProviderConnection) -> Mapping[str, Any]:
        raise NotImplementedError

    def discover_models(self, connection: ProviderConnection) -> ModelCatalog:
        payload = self.validate(connection)
        return ModelCatalog(
            self.parse_audio_models(payload),
            self.parse_text_models(payload),
        )

    def fetch_audio_models(self, connection: ProviderConnection) -> tuple[str, ...]:
        return self.discover_models(connection).audio_models

    def transcribe(self, request: TranscriptionRequest,
            connection: ProviderConnection) -> TranscriptionResult:
        self.require(ProviderCapability.AUDIO_TRANSCRIPTION)
        raise NotImplementedError

    def rewrite(self, request: RewriteRequest,
            connection: ProviderConnection) -> RewriteResult:
        self.require(ProviderCapability.TEXT_GENERATION)
        raise NotImplementedError

    def translate(self, request: TranslationRequest,
            connection: ProviderConnection) -> TranslationResult:
        self.require(ProviderCapability.TEXT_GENERATION)
        raise NotImplementedError


class GeminiAdapter(ProviderAdapter):
    """Adapter for Gemini generateContent and model discovery."""

    _VERSION = "v1beta"
    _TEXT_MODEL_EXCLUSIONS = (
        "transcribe", "transcription", "speech", "tts", "audio",
        "embedding", "embed", "moderation", "image", "realtime",
    )

    def _headers(self, connection: ProviderConnection) -> dict[str, str]:
        headers = {"x-goog-api-key": connection.api_key}
        if "generativelanguage.googleapis.com" not in connection.base_url.lower():
            headers["Authorization"] = f"Bearer {connection.api_key}"
        return headers

    @staticmethod
    def _model_id(entry: Mapping[str, Any]) -> str:
        model_id = str(entry.get("name") or entry.get("id") or "").strip()
        return model_id.removeprefix("models/")

    @staticmethod
    def _supports_generation(entry: Mapping[str, Any]) -> bool:
        methods = entry.get("supportedGenerationMethods")
        return methods is None or (
            isinstance(methods, list) and "generateContent" in methods)

    def _parse_models(self, payload: Mapping[str, Any]) -> tuple[str, ...]:
        models = {
            model_id
            for entry in _model_entries(payload)
            if self._supports_generation(entry)
            for model_id in (self._model_id(entry),)
            if model_id and "gemini" in model_id.lower()
        }
        return tuple(sorted(models, key=str.lower))

    def parse_audio_models(self, payload: Mapping[str, Any]) -> tuple[str, ...]:
        return self._parse_models(payload)

    def parse_text_models(self, payload: Mapping[str, Any]) -> tuple[str, ...]:
        return tuple(
            model for model in self._parse_models(payload)
            if not any(token in model.lower()
                for token in self._TEXT_MODEL_EXCLUSIONS)
        )

    def validate(self, connection: ProviderConnection) -> Mapping[str, Any]:
        self.require(ProviderCapability.MODEL_DISCOVERY)
        self._require_connection(connection)
        response = self.http.get(
            normalize_provider_url(connection.base_url, self._VERSION, "models"),
            headers=self._headers(connection), timeout=12,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ProviderResponseError(
                self.metadata.provider_id,
                f"{self.metadata.display_name} returned an invalid model catalog",
                ProviderCapability.MODEL_DISCOVERY,
            )
        return payload

    def _generate(self, model: str, connection: ProviderConnection,
            body: Mapping[str, Any], capability: ProviderCapability) -> str:
        self.require(capability)
        self._require_connection(connection)
        model_id = str(model or "").removeprefix("models/").strip()
        if not model_id:
            raise ProviderConfigurationError(
                self.metadata.provider_id,
                f"No {self.metadata.display_name} model",
                capability,
            )
        response = self.http.post(
            normalize_provider_url(
                connection.base_url, self._VERSION,
                f"models/{model_id}:generateContent"),
            headers=self._headers(connection), json=body, timeout=60,
        )
        response.raise_for_status()
        try:
            text = str(
                response.json()["candidates"][0]["content"]["parts"][0]["text"]
            ).strip()
        except (KeyError, IndexError, TypeError):
            text = ""
        if not text:
            raise ProviderResponseError(
                self.metadata.provider_id,
                f"{self.metadata.display_name} returned an empty response",
                capability,
            )
        return text

    def transcribe(self, request: TranscriptionRequest,
            connection: ProviderConnection) -> TranscriptionResult:
        self.require(ProviderCapability.MULTIMODAL_AUDIO)
        audio_bytes = (request.audio_bytes if request.audio_bytes is not None
                       else request.audio_path.read_bytes())
        audio_b64 = base64.b64encode(audio_bytes).decode()
        body = {
            "contents": [{"parts": [
                {"inlineData": {
                    "mimeType": audio_mime_type(request.audio_path),
                    "data": audio_b64,
                }},
                {"text": request.prompt},
            ]}],
            "systemInstruction": {"parts": [{"text": request.instruction}]},
            "generationConfig": {"temperature": request.temperature},
        }
        text = self._generate(
            request.model, connection, body,
            ProviderCapability.AUDIO_TRANSCRIPTION,
        )
        return TranscriptionResult(
            text, self.metadata.provider_id,
            request.model.removeprefix("models/"),
        )

    def _generate_text(self, model: str, instruction: str,
            source_message: str, temperature: float,
            connection: ProviderConnection) -> str:
        body = {
            "contents": [{"parts": [{"text": source_message}]}],
            "systemInstruction": {"parts": [{"text": instruction}]},
            "generationConfig": {"temperature": temperature},
        }
        return self._generate(
            model, connection, body, ProviderCapability.TEXT_GENERATION)

    def rewrite(self, request: RewriteRequest,
            connection: ProviderConnection) -> RewriteResult:
        text = self._generate_text(
            request.model, request.instruction, request.source_message,
            request.temperature, connection,
        )
        return RewriteResult(
            text, self.metadata.provider_id,
            request.model.removeprefix("models/"),
        )

    def translate(self, request: TranslationRequest,
            connection: ProviderConnection) -> TranslationResult:
        text = self._generate_text(
            request.model, request.instruction, request.source_message,
            request.temperature, connection,
        )
        return TranslationResult(
            text, self.metadata.provider_id,
            request.model.removeprefix("models/"), request.target_language,
        )


class OpenAICompatibleAdapter(ProviderAdapter):
    """Adapter shared by OpenAI, Groq, and future compatible providers."""

    _VERSION = "v1"
    _TEXT_MODEL_EXCLUSIONS = (
        "whisper", "transcribe", "transcription", "speech", "tts", "audio",
        "embedding", "embed", "moderation", "dall-e", "image", "realtime",
    )

    def __init__(self, metadata: ProviderMetadata, http: HttpClient,
            official_host: str = "", official_audio_models: tuple[str, ...] = (),
            audio_model_aliases: Mapping[str, str] | None = None):
        super().__init__(metadata, http)
        self.official_host = official_host.lower()
        self.official_audio_models = official_audio_models
        self.audio_model_aliases = {
            str(label).casefold(): model_id
            for label, model_id in (audio_model_aliases or {}).items()
        }

    @staticmethod
    def _model_id(entry: Mapping[str, Any]) -> str:
        return str(entry.get("id") or entry.get("name") or "").strip().removeprefix(
            "models/")

    def parse_audio_models(self, payload: Mapping[str, Any]) -> tuple[str, ...]:
        models = {
            model_id
            for entry in _model_entries(payload)
            for model_id in (self._model_id(entry),)
            if model_id and any(
                token in model_id.lower() for token in ("whisper", "transcribe"))
        }
        return tuple(sorted(models, key=str.lower))

    def parse_text_models(self, payload: Mapping[str, Any]) -> tuple[str, ...]:
        models = {
            model_id
            for entry in _model_entries(payload)
            for model_id in (self._model_id(entry),)
            if model_id and not any(
                token in model_id.lower() for token in self._TEXT_MODEL_EXCLUSIONS)
        }
        return tuple(sorted(models, key=str.lower))

    def canonical_audio_model(self, model: str) -> str:
        value = str(model or "").strip()
        return self.audio_model_aliases.get(value.casefold(), value)

    def _is_official(self, base_url: str) -> bool:
        return bool(self.official_host and self.official_host in base_url.lower())

    def validate(self, connection: ProviderConnection) -> Mapping[str, Any]:
        self.require(ProviderCapability.MODEL_DISCOVERY)
        self._require_connection(connection)
        response = self.http.get(
            normalize_provider_url(connection.base_url, self._VERSION, "models"),
            headers={"Authorization": f"Bearer {connection.api_key}"}, timeout=12,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ProviderResponseError(
                self.metadata.provider_id,
                f"{self.metadata.display_name} returned an invalid model catalog",
                ProviderCapability.MODEL_DISCOVERY,
            )
        return payload

    def discover_models(self, connection: ProviderConnection) -> ModelCatalog:
        catalog = super().discover_models(connection)
        audio_models = catalog.audio_models
        if not audio_models and self._is_official(connection.base_url):
            audio_models = self.official_audio_models
        return ModelCatalog(audio_models, catalog.text_models)

    def fetch_audio_models(self, connection: ProviderConnection) -> tuple[str, ...]:
        self.require(ProviderCapability.MODEL_DISCOVERY)
        if self._is_official(connection.base_url):
            return self.official_audio_models
        self._require_connection(connection)
        return super().fetch_audio_models(connection)

    def transcribe(self, request: TranscriptionRequest,
            connection: ProviderConnection) -> TranscriptionResult:
        self.require(ProviderCapability.AUDIO_TRANSCRIPTION)
        self._require_connection(connection)
        model = self.canonical_audio_model(request.model)
        if not model:
            raise ProviderConfigurationError(
                self.metadata.provider_id,
                f"No {self.metadata.display_name} audio model",
                ProviderCapability.AUDIO_TRANSCRIPTION,
            )
        # Snapshot bytes before entering the long request.  Requests' read
        # timeout is not a total wall-clock deadline, but the provider worker
        # no longer holds the recording path after this point.
        audio_bytes = (request.audio_bytes if request.audio_bytes is not None
                       else request.audio_path.read_bytes())
        audio_file = io.BytesIO(audio_bytes)
        try:
            response = self.http.post(
                normalize_provider_url(
                    connection.base_url, self._VERSION, "audio/transcriptions"),
                headers={"Authorization": f"Bearer {connection.api_key}"},
                files={"file": (
                    request.audio_path.name, audio_file,
                    audio_mime_type(request.audio_path),
                )},
                data={
                    "model": model,
                    "response_format": "json",
                    "language": request.language,
                },
                timeout=60,
            )
        finally:
            audio_file.close()
        response.raise_for_status()
        payload = response.json()
        text = str(payload.get("text", "")).strip() if isinstance(
            payload, Mapping) else ""
        if not text:
            raise ProviderResponseError(
                self.metadata.provider_id,
                f"{self.metadata.display_name} returned an empty transcription",
                ProviderCapability.AUDIO_TRANSCRIPTION,
            )
        return TranscriptionResult(text, self.metadata.provider_id, model)

    def _generate_text(self, model: str, instruction: str,
            source_message: str, temperature: float,
            connection: ProviderConnection) -> str:
        self.require(ProviderCapability.TEXT_GENERATION)
        self._require_connection(connection)
        model_id = str(model or "").strip()
        if not model_id:
            raise ProviderConfigurationError(
                self.metadata.provider_id,
                f"No {self.metadata.display_name} text model",
                ProviderCapability.TEXT_GENERATION,
            )
        body = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": source_message},
            ],
            "temperature": temperature,
        }
        response = self.http.post(
            normalize_provider_url(
                connection.base_url, self._VERSION, "chat/completions"),
            headers={"Authorization": f"Bearer {connection.api_key}"},
            json=body, timeout=60,
        )
        response.raise_for_status()
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            content = None
        text = content.strip() if isinstance(content, str) else ""
        if not text:
            raise ProviderResponseError(
                self.metadata.provider_id,
                f"{self.metadata.display_name} returned an empty text response",
                ProviderCapability.TEXT_GENERATION,
            )
        return text

    def rewrite(self, request: RewriteRequest,
            connection: ProviderConnection) -> RewriteResult:
        text = self._generate_text(
            request.model, request.instruction, request.source_message,
            request.temperature, connection,
        )
        return RewriteResult(text, self.metadata.provider_id, request.model)

    def translate(self, request: TranslationRequest,
            connection: ProviderConnection) -> TranslationResult:
        text = self._generate_text(
            request.model, request.instruction, request.source_message,
            request.temperature, connection,
        )
        return TranslationResult(
            text, self.metadata.provider_id, request.model,
            request.target_language,
        )
