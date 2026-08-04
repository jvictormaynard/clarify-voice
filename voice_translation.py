"""UI-independent foundation for a dedicated voice-translation workflow.

The existing translation workflow operates on selected text.  This module
keeps the voice-to-voice contract separate until a later change can connect a
global hotkey, recorder, and Windows clipboard adapter.  It owns only stable
language/configuration types, a provider request seam, and the state/publish
policy needed by those future adapters.

No Tk, Windows, persistence, or network code belongs here.  In particular, a
provider failure after transcription never destroys the raw transcript: the
policy returns it as an explicit ``COPY_ONLY`` fallback so a UI can offer a
safe copy/paste action without silently publishing the wrong text.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from provider_types import TranslationResult, TranscriptionResult
from provider_registry import PROVIDER_REGISTRY
from workflow_config import (
    WorkflowConfigurationError,
    WorkflowRoute,
    WorkflowScope,
    validate_workflow_route,
)


VOICE_TRANSLATION_SCHEMA_VERSION = 1
AUTO_LANGUAGE = "auto"
DEFAULT_TARGET_LANGUAGE = "en"

# Deliberately support the common BCP-47 language/script/region form without
# pretending to be a complete locale parser.  Extensions and private-use
# subtags can be added when a concrete provider requires them.
_LANGUAGE_TAG_RE = re.compile(
    r"^[A-Za-z]{2,3}(?:-[A-Za-z]{4})?(?:-(?:[A-Za-z]{2}|[0-9]{3}))?$"
)


class VoiceTranslationConfigurationError(ValueError):
    """A voice-translation language or provider route is not usable."""

    def __init__(
        self,
        message: str,
        *,
        field: str = "",
        provider_id: str = "",
        capability: Any = None,
    ) -> None:
        self.field = str(field or "")
        self.provider_id = str(provider_id or "")
        self.capability = capability
        super().__init__(message)


class UnsupportedVoiceTranslationSchemaError(
    VoiceTranslationConfigurationError
):
    """A persisted voice-translation payload is newer than this build."""


def normalize_language_tag(
    value: str,
    *,
    allow_auto: bool = False,
    field: str = "language",
) -> str:
    """Normalize one explicit language tag or the source ``auto`` sentinel."""

    text = str(value or "").strip().replace("_", "-")
    if allow_auto and text.casefold() == AUTO_LANGUAGE:
        return AUTO_LANGUAGE
    if not _LANGUAGE_TAG_RE.fullmatch(text):
        allowed = " or 'auto'" if allow_auto else ""
        raise VoiceTranslationConfigurationError(
            f"{field} must be a BCP-47 language tag{allowed}.",
            field=field,
        )
    parts = text.split("-")
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 4:
            normalized.append(part.title())
        else:
            normalized.append(part.upper())
    return "-".join(normalized)


def _schema_version(
    values: Mapping[str, Any],
    *,
    default: int = VOICE_TRANSLATION_SCHEMA_VERSION,
) -> int:
    raw = values.get("schema_version", values.get("version", default))
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise VoiceTranslationConfigurationError(
            "voice translation schema_version must be an integer.",
            field="schema_version",
        )
    if raw != VOICE_TRANSLATION_SCHEMA_VERSION:
        raise UnsupportedVoiceTranslationSchemaError(
            "unsupported voice translation schema version "
            f"{raw}; this build supports {VOICE_TRANSLATION_SCHEMA_VERSION}.",
            field="schema_version",
        )
    return raw


def _first_text(
    values: Mapping[str, Any],
    keys: tuple[str, ...],
    fallback: str,
) -> str:
    for key in keys:
        value = values.get(key)
        if isinstance(value, str):
            return value.strip()
    return fallback


@dataclass(frozen=True)
class VoiceTranslationLanguages:
    """Versioned source/target language selection for one voice workflow."""

    source_language: str = AUTO_LANGUAGE
    target_language: str = DEFAULT_TARGET_LANGUAGE
    schema_version: int = VOICE_TRANSLATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != VOICE_TRANSLATION_SCHEMA_VERSION:
            raise UnsupportedVoiceTranslationSchemaError(
                "unsupported voice translation language schema version "
                f"{self.schema_version}; this build supports "
                f"{VOICE_TRANSLATION_SCHEMA_VERSION}.",
                field="schema_version",
            )
        object.__setattr__(
            self,
            "source_language",
            normalize_language_tag(
                self.source_language,
                allow_auto=True,
                field="source_language",
            ),
        )
        object.__setattr__(
            self,
            "target_language",
            normalize_language_tag(
                self.target_language,
                allow_auto=False,
                field="target_language",
            ),
        )

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any] | None,
        *,
        default: "VoiceTranslationLanguages | None" = None,
    ) -> "VoiceTranslationLanguages":
        fallback = default or cls()
        if not isinstance(values, Mapping):
            return fallback
        version = _schema_version(values, default=fallback.schema_version)
        return cls(
            source_language=_first_text(
                values,
                ("source_language", "source", "from_language"),
                fallback.source_language,
            ),
            target_language=_first_text(
                values,
                ("target_language", "target", "to_language"),
                fallback.target_language,
            ),
            schema_version=version,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_language": self.source_language,
            "target_language": self.target_language,
        }


@dataclass(frozen=True)
class VoiceTranslationRoute:
    """Provider/model/prompt route dedicated to voice translation."""

    provider_id: str = "openai"
    model_id: str = "gpt-4o-mini"
    prompt: str = ""
    custom_endpoint: str = ""
    enabled: bool = True

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any] | None,
        *,
        default: "VoiceTranslationRoute | None" = None,
    ) -> "VoiceTranslationRoute":
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
            provider_id=text(
                "provider_id", "provider", fallback_value=fallback.provider_id
            ).lower(),
            model_id=text("model_id", "model", fallback_value=fallback.model_id),
            prompt=text("prompt", fallback_value=fallback.prompt),
            custom_endpoint=text(
                "custom_endpoint",
                "endpoint",
                "base_url",
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

    def diagnostic_mapping(self) -> dict[str, Any]:
        route = WorkflowRoute(
            provider_id=self.provider_id,
            model_id=self.model_id,
            prompt=self.prompt,
            custom_endpoint=self.custom_endpoint,
            enabled=self.enabled,
        )
        return route.without_prompt()


def validate_voice_translation_route(
    route: VoiceTranslationRoute | Mapping[str, Any],
    *,
    registry=PROVIDER_REGISTRY,
) -> VoiceTranslationRoute:
    """Validate the route through the text-generation capability registry."""

    if not isinstance(route, VoiceTranslationRoute):
        route = VoiceTranslationRoute.from_mapping(route)
    try:
        workflow_route = validate_workflow_route(
            WorkflowRoute(
                provider_id=route.provider_id,
                model_id=route.model_id,
                prompt=route.prompt,
                custom_endpoint=route.custom_endpoint,
                enabled=route.enabled,
            ),
            WorkflowScope.TRANSLATION,
            registry=registry,
        )
    except WorkflowConfigurationError as error:
        raise VoiceTranslationConfigurationError(
            str(error),
            field=error.field,
            provider_id=error.provider_id,
            capability=error.capability,
        ) from error
    return VoiceTranslationRoute(
        provider_id=workflow_route.provider_id,
        model_id=workflow_route.model_id,
        prompt=workflow_route.prompt,
        custom_endpoint=workflow_route.custom_endpoint,
        enabled=workflow_route.enabled,
    )


@dataclass(frozen=True)
class VoiceTranslationConfig:
    """Standalone, versioned config reserved for future persistence wiring."""

    languages: VoiceTranslationLanguages = field(
        default_factory=VoiceTranslationLanguages
    )
    route: VoiceTranslationRoute = field(default_factory=VoiceTranslationRoute)
    schema_version: int = VOICE_TRANSLATION_SCHEMA_VERSION

    @property
    def source_language(self) -> str:
        return self.languages.source_language

    @property
    def target_language(self) -> str:
        return self.languages.target_language

    def __post_init__(self) -> None:
        if self.schema_version != VOICE_TRANSLATION_SCHEMA_VERSION:
            raise UnsupportedVoiceTranslationSchemaError(
                "unsupported voice translation config schema version "
                f"{self.schema_version}; this build supports "
                f"{VOICE_TRANSLATION_SCHEMA_VERSION}.",
                field="schema_version",
            )
        if not isinstance(self.languages, VoiceTranslationLanguages):
            object.__setattr__(
                self,
                "languages",
                VoiceTranslationLanguages.from_mapping(self.languages),
            )
        if not isinstance(self.route, VoiceTranslationRoute):
            object.__setattr__(
                self,
                "route",
                VoiceTranslationRoute.from_mapping(self.route),
            )

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any] | None,
        *,
        default: "VoiceTranslationConfig | None" = None,
    ) -> "VoiceTranslationConfig":
        fallback = default or cls()
        if not isinstance(values, Mapping):
            return fallback
        version = _schema_version(values, default=fallback.schema_version)
        language_values = values.get("languages", values.get("language"))
        if not isinstance(language_values, Mapping):
            language_values = values
        route_values = values.get("route", values.get("provider_route"))
        if not isinstance(route_values, Mapping):
            # Flat fields are accepted for ergonomic config editors while the
            # canonical serialized shape remains nested under ``route``.
            route_values = values
        return cls(
            languages=VoiceTranslationLanguages.from_mapping(
                language_values,
                default=fallback.languages,
            ),
            route=VoiceTranslationRoute.from_mapping(
                route_values,
                default=fallback.route,
            ),
            schema_version=version,
        )

    def validate(self, *, registry=PROVIDER_REGISTRY) -> "VoiceTranslationConfig":
        return VoiceTranslationConfig(
            languages=self.languages,
            route=validate_voice_translation_route(self.route, registry=registry),
            schema_version=self.schema_version,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            **self.languages.to_mapping(),
            "route": self.route.to_mapping(),
        }

    def diagnostic_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "route": self.route.diagnostic_mapping(),
        }

    def execution_mapping(
        self,
        transcription_provider: str,
        *,
        local_asr_cloud_refinement: bool = False,
    ) -> dict[str, Any]:
        """Expose safe local/cloud policy for Settings and diagnostics.

        Voice translation intentionally owns only its text-generation route;
        transcription follows the application's typed audio route.  The
        refinement flag is surfaced explicitly so a local-ASR user can see
        whether the optional cloud pass is enabled rather than inferring it
        from provider names in UI code.
        """

        transcription = str(transcription_provider or "").strip().lower()
        translation = str(self.route.provider_id or "").strip().lower()
        return {
            "transcription_execution": (
                "local" if transcription == "local_asr" else "cloud"
            ),
            "translation_execution": (
                "local" if translation == "local_asr" else "cloud"
            ),
            "local_asr_cloud_refinement": bool(local_asr_cloud_refinement),
        }


def validate_voice_translation_config(
    config: VoiceTranslationConfig | Mapping[str, Any],
    *,
    registry=PROVIDER_REGISTRY,
) -> VoiceTranslationConfig:
    """Validate language and capability choices without network activity."""

    if not isinstance(config, VoiceTranslationConfig):
        config = VoiceTranslationConfig.from_mapping(config)
    return config.validate(registry=registry)


@dataclass(frozen=True)
class VoiceTranslationRequest:
    """Provider-neutral translation request carrying the dedicated route."""

    text: str
    source_language: str
    target_language: str
    route: VoiceTranslationRoute
    # Providers that expose cooperative cancellation can pass this token down
    # to their transport.  Keeping it on the provider-neutral request avoids
    # a second voice-specific API while fakes and older adapters can ignore it.
    cancel_event: Any = None

    @property
    def prompt(self) -> str:
        return self.route.prompt


class VoiceTranslationProvider(Protocol):
    """Minimal provider seam used by the future recorder/adapter bridge."""

    def transcribe(
        self, audio_source: Any, source_language: str
    ) -> TranscriptionResult | str: ...

    def translate(
        self, request: VoiceTranslationRequest
    ) -> TranslationResult | str: ...


class VoiceTranslationClipboard(Protocol):
    """Focus/clipboard boundary deliberately kept outside this module."""

    def capture_target(self) -> Any | None: ...

    def is_target_current(self, target: Any) -> bool: ...

    def owns_clipboard(self) -> bool: ...

    def publish(
        self,
        text: str,
        target: Any | None,
        disposition: "VoiceTranslationPublication",
    ) -> "VoiceTranslationPublication | None": ...


class VoiceTranslationClock(Protocol):
    def monotonic(self) -> float: ...


class SystemVoiceTranslationClock:
    def monotonic(self) -> float:
        return time.monotonic()


class VoiceTranslationPhase(str, Enum):
    READY = "ready"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    TRANSLATING = "translating"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VoiceTranslationPublication(str, Enum):
    NONE = "none"
    PASTED = "pasted"
    COPY_ONLY = "copy_only"
    REJECTED = "rejected"


@dataclass(frozen=True)
class VoiceTranslationState:
    """Immutable state snapshots suitable for a future UI renderer."""

    phase: VoiceTranslationPhase = VoiceTranslationPhase.READY
    operation_id: int = 0
    source_language: str = AUTO_LANGUAGE
    target_language: str = DEFAULT_TARGET_LANGUAGE
    raw_transcript: str = ""
    translated_text: str = ""
    published_text: str = ""
    publication: VoiceTranslationPublication = VoiceTranslationPublication.NONE
    publication_claimed: bool = False
    publication_reason: str = ""
    failure_code: str = ""
    failure_message: str = ""
    at_monotonic: float = 0.0


class VoiceTranslationBusyError(RuntimeError):
    """Raised when a state machine is asked to start over an active operation."""


class VoiceTranslationStateError(RuntimeError):
    """Raised when a workflow stage is called outside its legal phase."""


class VoiceTranslationStateMachine:
    """Thread-safe state transitions with transcript retention on failure."""

    _ACTIVE_PHASES = frozenset({
        VoiceTranslationPhase.RECORDING,
        VoiceTranslationPhase.TRANSCRIBING,
        VoiceTranslationPhase.TRANSLATING,
        VoiceTranslationPhase.PUBLISHING,
    })

    def __init__(
        self,
        config: VoiceTranslationConfig,
        *,
        clock: VoiceTranslationClock | None = None,
    ) -> None:
        self._config = config
        self._clock = clock or SystemVoiceTranslationClock()
        self._lock = threading.RLock()
        self._next_operation_id = 1
        self._state = VoiceTranslationState(
            source_language=config.source_language,
            target_language=config.target_language,
            at_monotonic=self._clock.monotonic(),
        )
        self._history: list[VoiceTranslationState] = [self._state]

    @property
    def state(self) -> VoiceTranslationState:
        with self._lock:
            return self._state

    @property
    def history(self) -> tuple[VoiceTranslationState, ...]:
        with self._lock:
            return tuple(self._history)

    def state_for_operation(self, operation_id: int) -> VoiceTranslationState:
        """Return the latest snapshot owned by ``operation_id``.

        A worker can finish after a newer operation has started. Returning the
        current state in that case would make the stale worker report the
        newer operation's result to its caller, so callers use this lookup for
        a stable, operation-owned outcome instead.
        """

        with self._lock:
            for snapshot in reversed(self._history):
                if snapshot.operation_id == operation_id:
                    return snapshot
        raise VoiceTranslationStateError(
            f"no voice translation state exists for operation {operation_id}"
        )

    def _set(self, **changes: Any) -> VoiceTranslationState:
        with self._lock:
            self._state = VoiceTranslationState(
                phase=changes.pop("phase", self._state.phase),
                operation_id=changes.pop("operation_id", self._state.operation_id),
                source_language=changes.pop(
                    "source_language", self._state.source_language
                ),
                target_language=changes.pop(
                    "target_language", self._state.target_language
                ),
                raw_transcript=changes.pop(
                    "raw_transcript", self._state.raw_transcript
                ),
                translated_text=changes.pop(
                    "translated_text", self._state.translated_text
                ),
                published_text=changes.pop(
                    "published_text", self._state.published_text
                ),
                publication=changes.pop(
                    "publication", self._state.publication
                ),
                publication_claimed=changes.pop(
                    "publication_claimed", self._state.publication_claimed
                ),
                publication_reason=changes.pop(
                    "publication_reason", self._state.publication_reason
                ),
                failure_code=changes.pop("failure_code", self._state.failure_code),
                failure_message=changes.pop(
                    "failure_message", self._state.failure_message
                ),
                at_monotonic=self._clock.monotonic(),
            )
            if changes:
                unknown = ", ".join(sorted(changes))
                raise TypeError(f"unknown voice translation state fields: {unknown}")
            self._history.append(self._state)
            return self._state

    def _require_phase(self, *phases: VoiceTranslationPhase) -> None:
        if self._state.phase not in phases:
            expected = ", ".join(phase.value for phase in phases)
            raise VoiceTranslationStateError(
                f"cannot transition from {self._state.phase.value}; expected "
                f"{expected}"
            )

    def _require_operation(self, operation_id: int) -> None:
        if operation_id != self._state.operation_id:
            raise VoiceTranslationStateError(
                "voice translation worker belongs to operation "
                f"{operation_id}, but current operation is "
                f"{self._state.operation_id}"
            )

    def begin(self) -> VoiceTranslationState:
        with self._lock:
            if self._state.phase in self._ACTIVE_PHASES:
                raise VoiceTranslationBusyError(
                    "another voice translation operation is still active"
                )
            operation_id = self._next_operation_id
            self._next_operation_id += 1
            return self._set(
                phase=VoiceTranslationPhase.RECORDING,
                operation_id=operation_id,
                source_language=self._config.source_language,
                target_language=self._config.target_language,
                raw_transcript="",
                translated_text="",
                published_text="",
                publication=VoiceTranslationPublication.NONE,
                publication_claimed=False,
                publication_reason="",
                failure_code="",
                failure_message="",
            )

    def begin_transcription(self, operation_id: int) -> VoiceTranslationState:
        with self._lock:
            self._require_operation(operation_id)
            self._require_phase(VoiceTranslationPhase.RECORDING)
            return self._set(phase=VoiceTranslationPhase.TRANSCRIBING)

    def transcript_received(
        self, text: str, operation_id: int
    ) -> VoiceTranslationState:
        with self._lock:
            self._require_operation(operation_id)
            self._require_phase(VoiceTranslationPhase.TRANSCRIBING)
            transcript = str(text or "").strip()
            if not transcript:
                return self.fail(
                    "empty_transcript",
                    "transcription returned no usable text",
                    operation_id=operation_id,
                )
            return self._set(
                phase=VoiceTranslationPhase.TRANSLATING,
                raw_transcript=transcript,
            )

    def translation_started(self, operation_id: int) -> VoiceTranslationState:
        with self._lock:
            self._require_operation(operation_id)
            self._require_phase(
                VoiceTranslationPhase.TRANSCRIBING,
                VoiceTranslationPhase.TRANSLATING,
            )
            return self._set(phase=VoiceTranslationPhase.TRANSLATING)

    def translation_received(
        self, text: str, operation_id: int
    ) -> VoiceTranslationState:
        with self._lock:
            self._require_operation(operation_id)
            self._require_phase(VoiceTranslationPhase.TRANSLATING)
            translated = str(text or "").strip()
            if not translated:
                return self.fail(
                    "empty_translation",
                    "translation returned no usable text",
                    operation_id=operation_id,
                )
            return self._set(
                phase=VoiceTranslationPhase.PUBLISHING,
                translated_text=translated,
            )

    def publishing(self, operation_id: int) -> VoiceTranslationState:
        with self._lock:
            self._require_operation(operation_id)
            self._require_phase(
                VoiceTranslationPhase.TRANSLATING,
                VoiceTranslationPhase.PUBLISHING,
            )
            return self._set(phase=VoiceTranslationPhase.PUBLISHING)

    def complete(
        self,
        decision: "PublicationDecision",
        *,
        operation_id: int,
        failure_code: str = "",
        failure_message: str = "",
    ) -> VoiceTranslationState:
        with self._lock:
            self._require_operation(operation_id)
            self._require_phase(VoiceTranslationPhase.PUBLISHING)
            phase = (
                VoiceTranslationPhase.FAILED
                if failure_code
                else VoiceTranslationPhase.COMPLETED
            )
            return self._set(
                phase=phase,
                published_text=decision.text,
                publication=decision.disposition,
                publication_reason=decision.reason,
                failure_code=failure_code,
                failure_message=failure_message,
            )

    def fail(
        self,
        code: str,
        message: str,
        *,
        operation_id: int,
        publication: VoiceTranslationPublication = VoiceTranslationPublication.NONE,
        publication_reason: str = "",
    ) -> VoiceTranslationState:
        with self._lock:
            self._require_operation(operation_id)
            if self._state.phase not in self._ACTIVE_PHASES:
                raise VoiceTranslationStateError(
                    f"cannot fail from {self._state.phase.value}"
                )
            return self._set(
                phase=VoiceTranslationPhase.FAILED,
                publication=publication,
                publication_reason=publication_reason,
                failure_code=str(code),
                failure_message=str(message),
            )

    def claim_publication(self, operation_id: int) -> VoiceTranslationState:
        """Atomically enter the non-cancellable publication barrier."""

        with self._lock:
            self._require_operation(operation_id)
            self._require_phase(VoiceTranslationPhase.PUBLISHING)
            if self._state.publication_claimed:
                raise VoiceTranslationStateError(
                    "voice translation publication is already claimed"
                )
            return self._set(publication_claimed=True)

    def cancel(
        self,
        message: str = "voice translation cancelled",
        *,
        operation_id: int | None = None,
    ) -> VoiceTranslationState:
        with self._lock:
            if operation_id is not None and operation_id != self._state.operation_id:
                return self._state
            if self._state.phase not in self._ACTIVE_PHASES:
                return self._state
            # Once publication is claimed, the external clipboard effect is a
            # terminal barrier.  Cancellation is intentionally ignored so a
            # blocked publish cannot later complete against CANCELLED state.
            if (
                self._state.phase is VoiceTranslationPhase.PUBLISHING
                and self._state.publication_claimed
            ):
                return self._state
            return self._set(
                phase=VoiceTranslationPhase.CANCELLED,
                failure_code="cancelled",
                failure_message=message,
            )


@dataclass(frozen=True)
class PublicationDecision:
    """An explicit, side-effect-free decision for the clipboard adapter."""

    disposition: VoiceTranslationPublication
    text: str = ""
    reason: str = ""


class VoiceTranslationPublicationPolicy:
    """Choose paste/copy-only without touching focus or clipboard state."""

    def decide(
        self,
        *,
        raw_transcript: str,
        translated_text: str,
        target_current: bool,
        clipboard_owned: bool,
        allow_paste: bool = True,
        translation_available: bool = True,
    ) -> PublicationDecision:
        raw = str(raw_transcript or "").strip()
        translated = str(translated_text or "").strip()
        if not raw:
            return PublicationDecision(
                VoiceTranslationPublication.NONE,
                reason="empty_transcript",
            )
        if not translation_available or not translated:
            return PublicationDecision(
                VoiceTranslationPublication.COPY_ONLY,
                raw,
                "translation_unavailable_raw_transcript",
            )
        if not allow_paste:
            return PublicationDecision(
                VoiceTranslationPublication.COPY_ONLY,
                translated,
                "paste_disabled",
            )
        if not target_current:
            return PublicationDecision(
                VoiceTranslationPublication.COPY_ONLY,
                translated,
                "target_not_current",
            )
        if not clipboard_owned:
            return PublicationDecision(
                VoiceTranslationPublication.COPY_ONLY,
                translated,
                "clipboard_not_owned",
            )
        return PublicationDecision(
            VoiceTranslationPublication.PASTED,
            translated,
            "target_and_clipboard_safe",
        )


class VoiceTranslationPublicationCoordinator:
    """Serialize publication effects while allowing processing to stay headless."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_operation_id: int | None = None

    @property
    def active_operation_id(self) -> int | None:
        with self._lock:
            return self._active_operation_id

    def claim(self, operation_id: int) -> bool:
        with self._lock:
            if self._active_operation_id is not None:
                return False
            self._active_operation_id = operation_id
            return True

    def release(self, operation_id: int) -> bool:
        with self._lock:
            if self._active_operation_id != operation_id:
                return False
            self._active_operation_id = None
            return True


# Global hotkeys may construct separate workflow objects.  They still need one
# publication lock unless a caller deliberately supplies a narrower coordinator
# for an isolated test or embedding.
_DEFAULT_PUBLICATION_COORDINATOR = VoiceTranslationPublicationCoordinator()


class VoiceTranslationWorkflow:
    """Run one synchronous, UI-free voice translation transaction.

    A later recorder/hotkey integration can call the same stages from worker
    callbacks.  This synchronous seam is intentionally small and deterministic
    enough for fake provider, clipboard, and clock tests.
    """

    def __init__(
        self,
        provider: VoiceTranslationProvider,
        clipboard: VoiceTranslationClipboard,
        config: VoiceTranslationConfig | Mapping[str, Any],
        *,
        clock: VoiceTranslationClock | None = None,
        policy: VoiceTranslationPublicationPolicy | None = None,
        coordinator: VoiceTranslationPublicationCoordinator | None = None,
        registry=PROVIDER_REGISTRY,
    ) -> None:
        self.config = validate_voice_translation_config(config, registry=registry)
        if not self.config.route.enabled:
            raise VoiceTranslationConfigurationError(
                "voice translation route is disabled", field="route.enabled"
            )
        self.provider = provider
        self.clipboard = clipboard
        self.policy = policy or VoiceTranslationPublicationPolicy()
        self.coordinator = (
            coordinator
            if coordinator is not None
            else _DEFAULT_PUBLICATION_COORDINATOR
        )
        self.state_machine = VoiceTranslationStateMachine(
            self.config,
            clock=clock,
        )

    @property
    def state(self) -> VoiceTranslationState:
        return self.state_machine.state

    @property
    def history(self) -> tuple[VoiceTranslationState, ...]:
        return self.state_machine.history

    def _state_for_operation(self, operation_id: int) -> VoiceTranslationState:
        """Keep a stale worker's return value scoped to its own operation."""

        try:
            return self.state_machine.state_for_operation(operation_id)
        except VoiceTranslationStateError:
            # Every workflow run begins with a state snapshot. Keep a safe
            # fallback for custom state-machine implementations that do not
            # retain history, without exposing a newer operation as this
            # worker's result.
            return VoiceTranslationState(
                phase=VoiceTranslationPhase.CANCELLED,
                operation_id=operation_id,
                source_language=self.config.source_language,
                target_language=self.config.target_language,
                failure_code="stale_operation",
                failure_message=(
                    "voice translation worker completed after its operation "
                    "was superseded"
                ),
            )

    @staticmethod
    def _text(result: TranscriptionResult | TranslationResult | str) -> str:
        value = getattr(result, "text", result)
        return str(value or "").strip()

    def _target_snapshot(self) -> tuple[Any | None, str]:
        try:
            return self.clipboard.capture_target(), ""
        except Exception as error:  # focus capture is a safe-copy fallback
            return None, f"target_capture_failed:{type(error).__name__}"

    def _cancelled(self, cancel_event: Any | None) -> bool:
        """Return whether an embedding requested cooperative cancellation."""

        if cancel_event is None:
            return False
        try:
            return bool(cancel_event.is_set())
        except (AttributeError, TypeError):
            try:
                return bool(cancel_event.cancelled)
            except (AttributeError, TypeError):
                return False

    def _cancel_operation(self, operation_id: int) -> VoiceTranslationState:
        """Publish a cancellation snapshot without leaking a newer run."""

        try:
            return self.state_machine.cancel(operation_id=operation_id)
        except VoiceTranslationStateError:
            return self._state_for_operation(operation_id)

    def _publish(
        self,
        target: Any | None,
        decision: PublicationDecision,
        *,
        operation_id: int,
        failure_code: str = "",
        failure_message: str = "",
    ) -> VoiceTranslationState:
        if decision.disposition is VoiceTranslationPublication.NONE:
            try:
                return self.state_machine.fail(
                    failure_code or "no_publication",
                    failure_message or decision.reason,
                    operation_id=operation_id,
                    publication=decision.disposition,
                    publication_reason=decision.reason,
                )
            except VoiceTranslationStateError:
                return self._state_for_operation(operation_id)
        try:
            # Claim the state-machine barrier before taking the shared
            # coordinator.  This ordering makes cancel-vs-publish atomic: a
            # cancellation either wins before this point or is ignored while
            # the external clipboard effect is in flight.
            self.state_machine.claim_publication(operation_id)
        except VoiceTranslationStateError:
            return self._state_for_operation(operation_id)
        if not self.coordinator.claim(operation_id):
            try:
                return self.state_machine.fail(
                    "publication_busy",
                    "another voice translation publication is in progress",
                    operation_id=operation_id,
                    publication=VoiceTranslationPublication.REJECTED,
                    publication_reason="publication_coordinator_busy",
                )
            except VoiceTranslationStateError:
                return self._state_for_operation(operation_id)
        try:
            published = self.clipboard.publish(
                decision.text,
                target,
                decision.disposition,
            )
        except Exception as error:
            try:
                return self.state_machine.fail(
                    "publication_failed",
                    str(error) or type(error).__name__,
                    operation_id=operation_id,
                    publication=VoiceTranslationPublication.NONE,
                    publication_reason="clipboard_publish_failed",
                )
            except VoiceTranslationStateError:
                return self._state_for_operation(operation_id)
        finally:
            self.coordinator.release(operation_id)
        try:
            effective_decision = decision
            if isinstance(published, VoiceTranslationPublication):
                effective_decision = PublicationDecision(
                    published,
                    decision.text,
                    "published_by_clipboard_adapter",
                )
            return self.state_machine.complete(
                effective_decision,
                operation_id=operation_id,
                failure_code=failure_code,
                failure_message=failure_message,
            )
        except VoiceTranslationStateError:
            return self._state_for_operation(operation_id)

    def run(
        self,
        audio_source: Any,
        *,
        allow_paste: bool = True,
        target: Any | None = None,
        cancel_event: Any | None = None,
    ) -> VoiceTranslationState:
        """Transcribe, translate, and apply the explicit publication policy."""

        started = self.state_machine.begin()
        operation_id = started.operation_id
        if self._cancelled(cancel_event):
            return self._cancel_operation(operation_id)
        target_capture_reason = ""
        if target is None:
            target, target_capture_reason = self._target_snapshot()
        try:
            self.state_machine.begin_transcription(operation_id)
            raw = self._text(
                self.provider.transcribe(audio_source, self.config.source_language)
            )
        except Exception as error:
            if self._cancelled(cancel_event):
                return self._cancel_operation(operation_id)
            try:
                return self.state_machine.fail(
                    "transcription_failed",
                    str(error) or type(error).__name__,
                    operation_id=operation_id,
                    publication_reason=target_capture_reason,
                )
            except VoiceTranslationStateError:
                return self._state_for_operation(operation_id)
        if self._cancelled(cancel_event):
            return self._cancel_operation(operation_id)
        if not raw:
            try:
                return self.state_machine.transcript_received(raw, operation_id)
            except VoiceTranslationStateError:
                return self._state_for_operation(operation_id)

        try:
            self.state_machine.transcript_received(raw, operation_id)
        except VoiceTranslationStateError:
            return self._state_for_operation(operation_id)
        current = self.state
        if current.operation_id != operation_id:
            return self._state_for_operation(operation_id)
        if current.phase is VoiceTranslationPhase.CANCELLED:
            return current
        if self._cancelled(cancel_event):
            return self._cancel_operation(operation_id)
        request = VoiceTranslationRequest(
            text=raw,
            source_language=self.config.source_language,
            target_language=self.config.target_language,
            route=self.config.route,
            cancel_event=cancel_event,
        )
        try:
            translated = self._text(self.provider.translate(request))
        except Exception as error:
            if self._cancelled(cancel_event):
                return self._cancel_operation(operation_id)
            try:
                self.state_machine.publishing(operation_id)
            except VoiceTranslationStateError:
                return self._state_for_operation(operation_id)
            decision = self.policy.decide(
                raw_transcript=raw,
                translated_text="",
                target_current=False,
                clipboard_owned=False,
                allow_paste=False,
                translation_available=False,
            )
            return self._publish(
                target,
                decision,
                operation_id=operation_id,
                failure_code="translation_failed",
                failure_message=str(error) or type(error).__name__,
            )
        if not translated:
            try:
                self.state_machine.publishing(operation_id)
            except VoiceTranslationStateError:
                return self._state_for_operation(operation_id)
            decision = self.policy.decide(
                raw_transcript=raw,
                translated_text="",
                target_current=False,
                clipboard_owned=False,
                allow_paste=False,
                translation_available=False,
            )
            return self._publish(
                target,
                decision,
                operation_id=operation_id,
                failure_code="empty_translation",
                failure_message="translation returned no usable text",
            )

        if self._cancelled(cancel_event):
            return self._cancel_operation(operation_id)

        try:
            self.state_machine.translation_received(translated, operation_id)
        except VoiceTranslationStateError:
            return self._state_for_operation(operation_id)
        target_current = False
        clipboard_owned = False
        if target is not None:
            try:
                target_current = bool(self.clipboard.is_target_current(target))
            except Exception:
                target_current = False
            if target_current:
                try:
                    clipboard_owned = bool(self.clipboard.owns_clipboard())
                except Exception:
                    clipboard_owned = False
        decision = self.policy.decide(
            raw_transcript=raw,
            translated_text=translated,
            target_current=target_current,
            clipboard_owned=clipboard_owned,
            allow_paste=allow_paste,
            translation_available=True,
        )
        reason = decision.reason
        if target_capture_reason and reason == "target_not_current":
            reason = target_capture_reason
            decision = PublicationDecision(decision.disposition, decision.text, reason)
        # Clipboard/focus inspection can run after the user requests Escape.
        # Re-check before claiming publication so a cancellation that wins
        # during that inspection cannot copy or paste a result.
        if self._cancelled(cancel_event):
            return self._cancel_operation(operation_id)
        return self._publish(target, decision, operation_id=operation_id)


__all__ = [
    "AUTO_LANGUAGE",
    "DEFAULT_TARGET_LANGUAGE",
    "PublicationDecision",
    "UnsupportedVoiceTranslationSchemaError",
    "VoiceTranslationBusyError",
    "VoiceTranslationClock",
    "VoiceTranslationClipboard",
    "VoiceTranslationConfig",
    "VoiceTranslationConfigurationError",
    "VoiceTranslationLanguages",
    "VoiceTranslationPhase",
    "VoiceTranslationPublication",
    "VoiceTranslationPublicationCoordinator",
    "VoiceTranslationPublicationPolicy",
    "VoiceTranslationProvider",
    "VoiceTranslationRequest",
    "VoiceTranslationRoute",
    "VoiceTranslationState",
    "VoiceTranslationStateError",
    "VoiceTranslationStateMachine",
    "VoiceTranslationWorkflow",
    "VOICE_TRANSLATION_SCHEMA_VERSION",
    "normalize_language_tag",
    "validate_voice_translation_config",
    "validate_voice_translation_route",
]
