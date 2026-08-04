"""Shared provider HTTP policy and privacy-safe local diagnostics."""

from __future__ import annotations

import json
import logging
import platform
import random
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from provider_types import (
    ProviderCapability,
    ProviderError as DomainProviderError,
)


TIMEOUTS = {
    "model_discovery": (3.05, 12.0),
    "validation": (3.05, 12.0),
    "transcription": (5.0, 90.0),
    "text_generation": (5.0, 60.0),
}
TRANSIENT_STATUS_CODES = frozenset((429, 502, 503, 504))
MAX_SAFE_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.25
BACKOFF_CAP_SECONDS = 4.0

_SENSITIVE_KEY = re.compile(
    r"(?:authorization|api[_-]?key|credential|secret|token|source[_-]?text|"
    r"transcript|recorded[_-]?text|rewritten[_-]?text|audio(?:[_-]?path)?|"
    r"dictionary(?:[_-]?context)?|vocabulary|snippet|request[_-]?body|"
    r"body|messages|contents|inline[_-]?data|data)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|authorization|credential)"
    r"([=:]\s*)[^\s,;]+"
)
_AUDIO_PATH = re.compile(
    r"(?i)(?:[A-Z]:\\|/)(?:[^\s\"']+[\\/])*[^\s\"']+\."
    r"(?:wav|mp3|m4a|flac|ogg|aac)"
)


class CancellationToken:
    """Thread-safe cooperative cancellation for provider operations."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)

    def raise_if_cancelled(
            self, provider: str = "", operation: str = "",
            operation_id: str | None = None) -> None:
        if self.cancelled:
            raise ProviderCancelledError(
                provider=provider, operation=operation,
                operation_id=operation_id)


_OPERATION_CAPABILITIES = {
    "model_discovery": ProviderCapability.MODEL_DISCOVERY,
    "validation": ProviderCapability.MODEL_DISCOVERY,
    "transcription": ProviderCapability.AUDIO_TRANSCRIPTION,
    "text_generation": ProviderCapability.TEXT_GENERATION,
}


class ProviderError(DomainProviderError):
    code = "provider_error"

    def __init__(
            self, *, provider: str = "", operation: str = "",
            status_code: int | None = None,
            operation_id: str | None = None) -> None:
        self.provider = provider
        self.operation = operation
        self.status_code = status_code
        self.operation_id = operation_id
        super().__init__(
            provider, self.code, _OPERATION_CAPABILITIES.get(operation))


class AuthenticationError(ProviderError):
    code = "authentication"


class RateLimitError(ProviderError):
    code = "rate_limit"


class QuotaError(RateLimitError):
    code = "quota"


class ProviderTimeoutError(ProviderError):
    code = "timeout"


class ServiceUnavailableError(ProviderError):
    code = "unavailable"


class InvalidModelError(ProviderError):
    code = "invalid_model"


class InvalidRequestError(ProviderError):
    code = "invalid_request"


class InvalidResponseError(ProviderError):
    code = "invalid_response"


class ProviderCancelledError(ProviderError):
    code = "cancelled"


class NetworkError(ProviderError):
    code = "network"


_MESSAGES = {
    "en": {
        "authentication": "Check the API key and provider permissions.",
        "rate_limit": "The provider rate limit was reached. Try again shortly.",
        "quota": "The provider quota is exhausted. Check billing or usage limits.",
        "timeout": "The provider took too long to respond. Try again.",
        "unavailable": "The provider is temporarily unavailable. Try again shortly.",
        "invalid_model": "The selected model is unavailable. Choose another model.",
        "invalid_request": "The provider rejected this request. Check the endpoint and model.",
        "invalid_response": "The provider returned an invalid response. Try again.",
        "cancelled": "The operation was cancelled.",
        "network": "Could not reach the provider. Check the connection and endpoint.",
        "provider_error": "The provider request failed.",
    },
    "pt": {
        "authentication": "Verifique a chave da API e as permissões do provedor.",
        "rate_limit": "O limite de requisições do provedor foi atingido. Tente novamente em breve.",
        "quota": "A cota do provedor acabou. Verifique o faturamento ou os limites de uso.",
        "timeout": "O provedor demorou demais para responder. Tente novamente.",
        "unavailable": "O provedor está temporariamente indisponível. Tente novamente em breve.",
        "invalid_model": "O modelo selecionado não está disponível. Escolha outro modelo.",
        "invalid_request": "O provedor rejeitou a requisição. Verifique o endpoint e o modelo.",
        "invalid_response": "O provedor retornou uma resposta inválida. Tente novamente.",
        "cancelled": "A operação foi cancelada.",
        "network": "Não foi possível acessar o provedor. Verifique a conexão e o endpoint.",
        "provider_error": "A requisição ao provedor falhou.",
    },
    "es": {
        "authentication": "Comprueba la clave de API y los permisos del proveedor.",
        "rate_limit": "Se alcanzó el límite de solicitudes. Inténtalo de nuevo en breve.",
        "quota": "La cuota del proveedor se agotó. Comprueba la facturación o los límites.",
        "timeout": "El proveedor tardó demasiado en responder. Inténtalo de nuevo.",
        "unavailable": "El proveedor no está disponible temporalmente. Inténtalo en breve.",
        "invalid_model": "El modelo seleccionado no está disponible. Elige otro modelo.",
        "invalid_request": "El proveedor rechazó la solicitud. Comprueba el endpoint y el modelo.",
        "invalid_response": "El proveedor devolvió una respuesta no válida. Inténtalo de nuevo.",
        "cancelled": "La operación fue cancelada.",
        "network": "No se pudo acceder al proveedor. Comprueba la conexión y el endpoint.",
        "provider_error": "La solicitud al proveedor falló.",
    },
    "de": {
        "authentication": "API-Schlüssel und Berechtigungen des Anbieters prüfen.",
        "rate_limit": "Das Anfragelimit wurde erreicht. Bitte gleich erneut versuchen.",
        "quota": "Das Anbieter-Kontingent ist aufgebraucht. Abrechnung oder Limits prüfen.",
        "timeout": "Der Anbieter hat zu lange nicht geantwortet. Bitte erneut versuchen.",
        "unavailable": "Der Anbieter ist vorübergehend nicht verfügbar. Bitte gleich erneut versuchen.",
        "invalid_model": "Das ausgewählte Modell ist nicht verfügbar. Bitte ein anderes wählen.",
        "invalid_request": "Der Anbieter hat die Anfrage abgelehnt. Endpoint und Modell prüfen.",
        "invalid_response": "Der Anbieter hat eine ungültige Antwort gesendet. Bitte erneut versuchen.",
        "cancelled": "Der Vorgang wurde abgebrochen.",
        "network": "Der Anbieter ist nicht erreichbar. Verbindung und Endpoint prüfen.",
        "provider_error": "Die Anfrage an den Anbieter ist fehlgeschlagen.",
    },
    "ru": {
        "authentication": "Проверьте ключ API и разрешения провайдера.",
        "rate_limit": "Достигнут лимит запросов. Повторите попытку позже.",
        "quota": "Квота провайдера исчерпана. Проверьте оплату или лимиты.",
        "timeout": "Провайдер не ответил вовремя. Повторите попытку.",
        "unavailable": "Провайдер временно недоступен. Повторите попытку позже.",
        "invalid_model": "Выбранная модель недоступна. Выберите другую модель.",
        "invalid_request": "Провайдер отклонил запрос. Проверьте endpoint и модель.",
        "invalid_response": "Провайдер вернул недопустимый ответ. Повторите попытку.",
        "cancelled": "Операция отменена.",
        "network": "Не удалось связаться с провайдером. Проверьте соединение и endpoint.",
        "provider_error": "Запрос к провайдеру завершился ошибкой.",
    },
}


def localized_error_message(error: ProviderError, language: str = "en") -> str:
    messages = _MESSAGES.get(language, _MESSAGES["en"])
    return messages.get(error.code, messages["provider_error"])


def redact_sensitive(value: Any, key: str = "") -> Any:
    """Recursively redact credentials and user-content fields."""
    if key and _SENSITIVE_KEY.search(str(key)):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): redact_sensitive(item, str(item_key))
                for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        redacted = _BEARER.sub("Bearer [REDACTED]", value)
        redacted = _SECRET_ASSIGNMENT.sub(
            lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", redacted)
        return _AUDIO_PATH.sub("[REDACTED_AUDIO_PATH]", redacted)
    return value


class _BestEffortRotatingFileHandler(RotatingFileHandler):
    """A file sink whose diagnostics failures stay silent and non-fatal."""

    def handleError(self, _record):
        return


class SafeRotatingLogger:
    def __init__(self, directory: Path, *, max_bytes: int = 512 * 1024,
                 backup_count: int = 3) -> None:
        self.directory = Path(directory)
        self.path = self.directory / "provider.log"
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._logger: logging.Logger | None = None
        self._lock = threading.Lock()

    def _get_logger(self) -> logging.Logger:
        with self._lock:
            if self._logger is not None:
                return self._logger
            self.directory.mkdir(parents=True, exist_ok=True)
            logger = logging.getLogger(f"clarifyvoice.provider.{id(self)}")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            handler = _BestEffortRotatingFileHandler(
                self.path, maxBytes=self.max_bytes,
                backupCount=self.backup_count, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
            self._logger = logger
            return logger

    def write(self, event: dict[str, Any]) -> None:
        # Diagnostics are strictly best-effort. A read-only profile, a full
        # disk, or a failed rollover must never change provider behavior.
        try:
            safe_event = redact_sensitive({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **event,
            })
            self._get_logger().info(json.dumps(
                safe_event, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")))
        except Exception:
            return

    def close(self) -> None:
        with self._lock:
            if self._logger is None:
                return
            for handler in tuple(self._logger.handlers):
                handler.flush()
                handler.close()
                self._logger.removeHandler(handler)
            self._logger = None


def _response_error_code(response: requests.Response) -> str:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error", payload)
    if not isinstance(error, dict):
        return ""
    return str(error.get("code") or error.get("type") or "")[:200].casefold()


def _response_error_status(response: requests.Response) -> str:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error", payload)
    if not isinstance(error, dict):
        return ""
    return str(error.get("status") or "")[:200].casefold()


def _is_permanent_quota(classification: str) -> bool:
    if "insufficient_quota" in classification:
        return True
    return bool(re.search(
        r"(?:\b(?:billing|credit)[\s_-]+(?:hard[\s_-]+limit|"
        r"limit[\s_-]+reached|exhausted)\b|"
        r"\b(?:hard[\s_-]+limit|limit[\s_-]+reached)[\s_-]+"
        r"(?:billing|credit)\b)",
        classification,
    ))


_MODEL_REQUEST_OPERATIONS = frozenset(("transcription", "text_generation"))
_STRUCTURED_MODEL_ERRORS = frozenset(("model_not_found", "invalid_model"))
_MODEL_NOT_FOUND_MESSAGE = re.compile(
    r"\bmodel\s+(?:[\"'`]([^\"'`\r\n]{1,128})[\"'`]|"
    r"([A-Za-z0-9][\w./:@-]{0,127}))\s+(?:was\s+)?not\s+found\b",
)
_GEMINI_MODEL_NOT_FOUND_MESSAGE = re.compile(
    r"\bmodels/([A-Za-z0-9][\w./:@-]{0,127})\s+is\s+not\s+found\b",
)
_NON_MODEL_ROUTE_NAMES = frozenset((
    "api", "endpoint", "list", "models", "path", "resource", "route",
))


def _is_invalid_model_response(
        operation: str, error_code: str, response_text: str,
        error_status: str = "") -> bool:
    """Classify only model-targeting failures as unavailable models."""
    if operation not in _MODEL_REQUEST_OPERATIONS:
        return False
    if error_code in _STRUCTURED_MODEL_ERRORS:
        return True
    match = _MODEL_NOT_FOUND_MESSAGE.search(response_text)
    if match is None:
        return (
            error_status == "not_found"
            and _GEMINI_MODEL_NOT_FOUND_MESSAGE.search(response_text) is not None
        )
    model_name = (match.group(1) or match.group(2) or "").strip().casefold()
    if model_name not in _NON_MODEL_ROUTE_NAMES:
        return True
    if error_status != "not_found":
        return False
    return _GEMINI_MODEL_NOT_FOUND_MESSAGE.search(response_text) is not None


def _http_error(response: requests.Response, provider: str,
                operation: str, operation_id: str) -> ProviderError:
    status = int(response.status_code)
    error_code = _response_error_code(response)
    error_status = _response_error_status(response)
    try:
        classification_text = str(getattr(response, "text", ""))[:2000].casefold()
    except Exception:
        classification_text = ""
    classification = " ".join((error_code, error_status, classification_text))
    details = {
        "provider": provider,
        "operation": operation,
        "status_code": status,
        "operation_id": operation_id,
    }
    headers = getattr(response, "headers", {}) or {}
    try:
        retry_after_seconds = _retry_after_seconds(
            headers.get("Retry-After"))
    except (AttributeError, TypeError, ValueError):
        retry_after_seconds = None

    def finish(error: ProviderError) -> ProviderError:
        if retry_after_seconds is not None:
            # Keep the typed error content-free while carrying the server's
            # bounded wait hint to an outer operation-level retry policy.
            setattr(error, "retry_after_seconds", retry_after_seconds)
        return error

    if status in (401, 403):
        return finish(AuthenticationError(**details))
    if status == 429:
        if _is_permanent_quota(classification):
            return finish(QuotaError(**details))
        return finish(RateLimitError(**details))
    if status in (408,):
        return finish(ProviderTimeoutError(**details))
    if status in (502, 503, 504) or status >= 500:
        return finish(ServiceUnavailableError(**details))
    if status in (400, 404) and _is_invalid_model_response(
            operation, error_code, classification_text, error_status):
        return finish(InvalidModelError(**details))
    if status in (400, 404, 405, 409, 415, 422):
        return finish(InvalidRequestError(**details))
    return finish(ProviderError(**details))


def _network_error(error: BaseException, provider: str,
                   operation: str, operation_id: str) -> ProviderError:
    if isinstance(error, requests.Timeout):
        return ProviderTimeoutError(
            provider=provider, operation=operation,
            operation_id=operation_id)
    return NetworkError(
        provider=provider, operation=operation,
        operation_id=operation_id)


def _retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return max(0.0, (retry_at - current).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


class ProviderHttpClient:
    """One provider HTTP boundary with operation-specific policy.

    GET/HEAD/OPTIONS requests use at most three attempts. Requests that may
    create work or charges use one attempt because they have no idempotency key.
    """

    def __init__(self, *, session: requests.Session | None = None,
                 logger: SafeRotatingLogger | None = None,
                 random_fn=random.random, sleeper=None) -> None:
        self.session = session or requests.Session()
        self.logger = logger
        self._random = random_fn
        self._sleeper = sleeper

    @staticmethod
    def _can_retry(method: str, safe_to_retry: bool | None) -> bool:
        method_is_safe = method.upper() in ("GET", "HEAD", "OPTIONS")
        return method_is_safe and safe_to_retry is not False

    def _backoff(self, attempt: int, retry_after: str | None = None) -> float:
        announced = _retry_after_seconds(retry_after)
        if announced is not None:
            return min(BACKOFF_CAP_SECONDS, announced)
        base = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        jitter = base * 0.25 * self._random()
        return min(BACKOFF_CAP_SECONDS, base + jitter)

    @staticmethod
    def _host(url: str) -> str:
        try:
            return urlsplit(url).hostname or ""
        except (TypeError, ValueError):
            # Malformed custom endpoints must not mask the typed transport
            # error while diagnostics are being assembled.
            return ""

    def _log(self, **event: Any) -> None:
        if self.logger is not None:
            # Keep custom/injected sinks subject to the same best-effort
            # boundary as SafeRotatingLogger.
            try:
                self.logger.write(event)
            except Exception:
                return

    def _sleep(self, delay: float, token: CancellationToken | None,
               provider: str, operation: str, operation_id: str) -> None:
        if self._sleeper is not None:
            self._sleeper(delay)
            if token is not None:
                token.raise_if_cancelled(provider, operation, operation_id)
            return
        if token is not None:
            if token.wait(delay):
                raise ProviderCancelledError(
                    provider=provider, operation=operation,
                    operation_id=operation_id)
        else:
            time.sleep(delay)

    def request(
            self, method: str, url: str, *, provider: str, operation: str,
            cancel_token: CancellationToken | None = None,
            safe_to_retry: bool | None = None, **kwargs: Any) -> requests.Response:
        method = method.upper()
        if operation not in TIMEOUTS:
            raise ValueError(f"unknown provider HTTP operation: {operation}")
        if "timeout" in kwargs:
            raise ValueError("timeouts are owned by ProviderHttpClient")
        retryable = self._can_retry(method, safe_to_retry)
        attempts = MAX_SAFE_ATTEMPTS if retryable else 1
        sender = getattr(self.session, method.lower())
        operation_id = secrets.token_hex(6)

        for attempt in range(1, attempts + 1):
            if cancel_token is not None:
                try:
                    cancel_token.raise_if_cancelled(
                        provider, operation, operation_id)
                except ProviderCancelledError:
                    self._log(
                        event="provider_http_error", provider=provider,
                        operation=operation, operation_id=operation_id,
                        method=method, host=self._host(url), attempt=attempt,
                        max_attempts=attempts, error_type="cancelled")
                    raise
            try:
                response = sender(url, timeout=TIMEOUTS[operation], **kwargs)
            except requests.RequestException as error:
                typed_error = _network_error(
                    error, provider, operation, operation_id)
                should_retry = (
                    retryable
                    and isinstance(error, (requests.ConnectionError, requests.Timeout))
                    and attempt < attempts
                )
                delay = self._backoff(attempt) if should_retry else None
                self._log(
                    event="provider_http_error", provider=provider,
                    operation=operation, operation_id=operation_id,
                    method=method, host=self._host(url),
                    attempt=attempt, max_attempts=attempts,
                    error_type=typed_error.code, retry_delay_seconds=delay)
                if not should_retry:
                    raise typed_error from error
                self._sleep(
                    delay or 0.0, cancel_token, provider, operation,
                    operation_id)
                continue

            if cancel_token is not None and cancel_token.cancelled:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                self._log(
                    event="provider_http_error", provider=provider,
                    operation=operation, operation_id=operation_id,
                    method=method, host=self._host(url), attempt=attempt,
                    max_attempts=attempts, error_type="cancelled")
                raise ProviderCancelledError(
                    provider=provider, operation=operation,
                    operation_id=operation_id)
            status = int(response.status_code)
            if 200 <= status < 300:
                setattr(response, "_clarify_operation_id", operation_id)
                return response

            typed_error = _http_error(
                response, provider, operation, operation_id)
            should_retry = (
                retryable and status in TRANSIENT_STATUS_CODES
                and not isinstance(typed_error, QuotaError)
                and attempt < attempts)
            headers = getattr(response, "headers", {}) or {}
            delay = self._backoff(attempt, headers.get("Retry-After")) \
                if should_retry else None
            self._log(
                event="provider_http_error", provider=provider,
                operation=operation, operation_id=operation_id,
                method=method, host=self._host(url),
                attempt=attempt, max_attempts=attempts,
                status_code=status,
                error_type=typed_error.code, retry_delay_seconds=delay)
            if not should_retry:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                raise typed_error
            close = getattr(response, "close", None)
            if callable(close):
                close()
            self._sleep(
                delay or 0.0, cancel_token, provider, operation,
                operation_id)

        raise AssertionError("provider HTTP retry loop exhausted unexpectedly")

    def invalid_response(self, response: requests.Response, *, provider: str,
            operation: str) -> InvalidResponseError:
        operation_id = getattr(response, "_clarify_operation_id", None)
        typed_error = InvalidResponseError(
            provider=provider, operation=operation,
            status_code=getattr(response, "status_code", None),
            operation_id=operation_id)
        self._log(
            event="provider_http_error", provider=provider,
            operation=operation, operation_id=operation_id,
            status_code=getattr(response, "status_code", None),
            error_type=typed_error.code)
        return typed_error

    def json(self, response: requests.Response, *, provider: str,
             operation: str) -> Any:
        try:
            return response.json()
        except (TypeError, ValueError) as error:
            typed_error = self.invalid_response(
                response, provider=provider, operation=operation)
            raise typed_error from error


def export_diagnostics(
        destination: Path, *, log_directory: Path,
        application_version: str, limit: int = 200) -> Path:
    """Export safe runtime metadata and parsed redacted error events."""
    events: list[dict[str, Any]] = []
    log_directory = Path(log_directory)
    candidates = sorted(log_directory.glob("provider.log*"), reverse=True)
    for path in candidates:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(event, dict):
                events.append(redact_sensitive(event))
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "application": {"name": "ClarifyVoice", "version": application_version},
        "environment": {
            "platform": platform.system(),
            "platform_release": platform.release(),
            "python_version": platform.python_version(),
            "requests_version": requests.__version__,
            "executable_kind": "frozen" if getattr(sys, "frozen", False) else "source",
        },
        "recent_errors": events[-limit:],
    }
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(redact_sensitive(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return destination
