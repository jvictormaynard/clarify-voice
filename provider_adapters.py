"""Provider adapters with no dependency on the desktop UI."""

from __future__ import annotations

import base64
import io
import mimetypes
from pathlib import Path
from typing import Any, Mapping

from provider_http import InvalidResponseError
from provider_types import (
    HttpClient,
    ModelCatalog,
    ProviderCapability,
    ProviderConfigurationError,
    ProviderConnection,
    ProviderMetadata,
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
    if not isinstance(payload, Mapping):
        return []
    for key in ("models", "data"):
        entries = payload.get(key)
        if isinstance(entries, list) and all(
                isinstance(entry, Mapping) for entry in entries):
            return list(entries)
    return []


def _valid_catalog_payload(payload: Any, keys: str | tuple[str, ...]) -> bool:
    if not isinstance(payload, Mapping):
        return False
    expected_keys = (keys,) if isinstance(keys, str) else keys
    return any(
        key in payload
        and isinstance(payload[key], list)
        and all(isinstance(entry, Mapping) for entry in payload[key])
        for key in expected_keys
    )


def _invalid_response_error(http, response, provider: str, operation: str):
    factory = getattr(http, "invalid_response", None)
    if callable(factory):
        return factory(response, provider=provider, operation=operation)
    return InvalidResponseError(
        provider=provider, operation=operation,
        status_code=getattr(response, "status_code", None),
        operation_id=getattr(response, "_clarify_operation_id", None),
    )


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

    def validate(self, connection: ProviderConnection, cancel_token=None,
            *, operation: str = "validation") -> Mapping[str, Any]:
        raise NotImplementedError

    def discover_models(self, connection: ProviderConnection,
            cancel_token=None) -> ModelCatalog:
        payload = self.validate(
            connection, cancel_token, operation="model_discovery")
        return ModelCatalog(
            self.parse_audio_models(payload),
            self.parse_text_models(payload),
        )

    def fetch_audio_models(self, connection: ProviderConnection,
            cancel_token=None) -> tuple[str, ...]:
        return self.discover_models(connection, cancel_token).audio_models

    def transcribe(self, request: TranscriptionRequest,
            connection: ProviderConnection, cancel_token=None) -> TranscriptionResult:
        self.require(ProviderCapability.AUDIO_TRANSCRIPTION)
        raise NotImplementedError

    def rewrite(self, request: RewriteRequest,
            connection: ProviderConnection, cancel_token=None) -> RewriteResult:
        self.require(ProviderCapability.TEXT_GENERATION)
        raise NotImplementedError

    def translate(self, request: TranslationRequest,
            connection: ProviderConnection, cancel_token=None) -> TranslationResult:
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

    def validate(self, connection: ProviderConnection, cancel_token=None,
            *, operation: str = "validation") -> Mapping[str, Any]:
        self.require(ProviderCapability.MODEL_DISCOVERY)
        self._require_connection(connection)
        response = self.http.request(
            "GET",
            normalize_provider_url(connection.base_url, self._VERSION, "models"),
            provider=self.metadata.provider_id, operation=operation,
            cancel_token=cancel_token, headers=self._headers(connection),
        )
        payload = self.http.json(
            response, provider=self.metadata.provider_id, operation=operation)
        if not _valid_catalog_payload(payload, "models"):
            raise _invalid_response_error(
                self.http, response, self.metadata.provider_id, operation)
        return payload

    def _generate(self, model: str, connection: ProviderConnection,
            body: Mapping[str, Any], capability: ProviderCapability,
            operation: str, cancel_token=None) -> str:
        self.require(capability)
        self._require_connection(connection)
        model_id = str(model or "").removeprefix("models/").strip()
        if not model_id:
            raise ProviderConfigurationError(
                self.metadata.provider_id,
                f"No {self.metadata.display_name} model",
                capability,
            )
        response = self.http.request(
            "POST",
            normalize_provider_url(
                connection.base_url, self._VERSION,
                f"models/{model_id}:generateContent"),
            provider=self.metadata.provider_id, operation=operation,
            cancel_token=cancel_token, safe_to_retry=False,
            headers=self._headers(connection), json=body,
        )
        payload = self.http.json(
            response, provider=self.metadata.provider_id, operation=operation)
        try:
            text = str(
                payload["candidates"][0]["content"]["parts"][0]["text"]
            ).strip()
        except (KeyError, IndexError, TypeError):
            text = ""
        if not text:
            raise _invalid_response_error(
                self.http, response, self.metadata.provider_id, operation)
        return text

    def transcribe(self, request: TranscriptionRequest,
            connection: ProviderConnection, cancel_token=None) -> TranscriptionResult:
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
                {"text": request.effective_prompt()},
            ]}],
            "systemInstruction": {"parts": [{"text": request.instruction}]},
            "generationConfig": {"temperature": request.temperature},
        }
        text = self._generate(
            request.model, connection, body,
            ProviderCapability.AUDIO_TRANSCRIPTION,
            "transcription", cancel_token,
        )
        return TranscriptionResult(
            text, self.metadata.provider_id,
            request.model.removeprefix("models/"),
        )

    def _generate_text(self, model: str, instruction: str,
            source_message: str, temperature: float,
            connection: ProviderConnection, cancel_token=None) -> str:
        body = {
            "contents": [{"parts": [{"text": source_message}]}],
            "systemInstruction": {"parts": [{"text": instruction}]},
            "generationConfig": {"temperature": temperature},
        }
        return self._generate(
            model, connection, body, ProviderCapability.TEXT_GENERATION,
            "text_generation", cancel_token)

    def rewrite(self, request: RewriteRequest,
            connection: ProviderConnection, cancel_token=None) -> RewriteResult:
        text = self._generate_text(
            request.model, request.instruction, request.source_message,
            request.temperature, connection, cancel_token,
        )
        return RewriteResult(
            text, self.metadata.provider_id,
            request.model.removeprefix("models/"),
        )

    def translate(self, request: TranslationRequest,
            connection: ProviderConnection, cancel_token=None) -> TranslationResult:
        text = self._generate_text(
            request.model, request.instruction, request.source_message,
            request.temperature, connection, cancel_token,
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

    def validate(self, connection: ProviderConnection, cancel_token=None,
            *, operation: str = "validation") -> Mapping[str, Any]:
        self.require(ProviderCapability.MODEL_DISCOVERY)
        self._require_connection(connection)
        response = self.http.request(
            "GET",
            normalize_provider_url(connection.base_url, self._VERSION, "models"),
            provider=self.metadata.provider_id, operation=operation,
            cancel_token=cancel_token,
            headers={"Authorization": f"Bearer {connection.api_key}"},
        )
        payload = self.http.json(
            response, provider=self.metadata.provider_id, operation=operation)
        if not _valid_catalog_payload(payload, ("models", "data")):
            raise _invalid_response_error(
                self.http, response, self.metadata.provider_id, operation)
        return payload

    def discover_models(self, connection: ProviderConnection,
            cancel_token=None) -> ModelCatalog:
        catalog = super().discover_models(connection, cancel_token)
        audio_models = catalog.audio_models
        if not audio_models and self._is_official(connection.base_url):
            audio_models = self.official_audio_models
        return ModelCatalog(audio_models, catalog.text_models)

    def fetch_audio_models(self, connection: ProviderConnection,
            cancel_token=None) -> tuple[str, ...]:
        self.require(ProviderCapability.MODEL_DISCOVERY)
        if self._is_official(connection.base_url):
            return self.official_audio_models
        self._require_connection(connection)
        return super().fetch_audio_models(connection, cancel_token)

    def transcribe(self, request: TranscriptionRequest,
            connection: ProviderConnection, cancel_token=None) -> TranscriptionResult:
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
            transcription_data = {
                "model": model,
                "response_format": "json",
                "prompt": request.effective_prompt(),
            }
            # Whisper detects the language when the optional hint is omitted;
            # sending ``language=auto`` is rejected by OpenAI-compatible APIs.
            if str(request.language or "").strip():
                transcription_data["language"] = request.language
            response = self.http.request(
                "POST",
                normalize_provider_url(
                    connection.base_url, self._VERSION, "audio/transcriptions"),
                provider=self.metadata.provider_id, operation="transcription",
                cancel_token=cancel_token, safe_to_retry=False,
                headers={"Authorization": f"Bearer {connection.api_key}"},
                files={"file": (
                    request.audio_path.name, audio_file,
                    audio_mime_type(request.audio_path),
                )},
                data=transcription_data,
            )
        finally:
            audio_file.close()
        payload = self.http.json(
            response, provider=self.metadata.provider_id,
            operation="transcription")
        text = str(payload.get("text", "")).strip() if isinstance(
            payload, Mapping) else ""
        if not text:
            raise _invalid_response_error(
                self.http, response, self.metadata.provider_id, "transcription")
        return TranscriptionResult(text, self.metadata.provider_id, model)

    def _generate_text(self, model: str, instruction: str,
            source_message: str, temperature: float,
            connection: ProviderConnection, cancel_token=None) -> str:
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
        response = self.http.request(
            "POST",
            normalize_provider_url(
                connection.base_url, self._VERSION, "chat/completions"),
            provider=self.metadata.provider_id, operation="text_generation",
            cancel_token=cancel_token, safe_to_retry=False,
            headers={"Authorization": f"Bearer {connection.api_key}"},
            json=body,
        )
        payload = self.http.json(
            response, provider=self.metadata.provider_id,
            operation="text_generation")
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            content = None
        text = content.strip() if isinstance(content, str) else ""
        if not text:
            raise _invalid_response_error(
                self.http, response,
                self.metadata.provider_id, "text_generation")
        return text

    def rewrite(self, request: RewriteRequest,
            connection: ProviderConnection, cancel_token=None) -> RewriteResult:
        text = self._generate_text(
            request.model, request.instruction, request.source_message,
            request.temperature, connection, cancel_token,
        )
        return RewriteResult(text, self.metadata.provider_id, request.model)

    def translate(self, request: TranslationRequest,
            connection: ProviderConnection, cancel_token=None) -> TranslationResult:
        text = self._generate_text(
            request.model, request.instruction, request.source_message,
            request.temperature, connection, cancel_token,
        )
        return TranslationResult(
            text, self.metadata.provider_id, request.model,
            request.target_language,
        )
