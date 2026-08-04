"""UI-independent microphone selection and recording boundary policies.

This module is the first, deliberately small seam for issue #52.  It does
not open an audio stream, stop a recorder, or update the Tk UI.  A platform
adapter can turn its device inventory into :class:`MicrophoneDevice` values,
persist :class:`MicrophoneSettings`, and use :class:`RecordingBoundaryPolicy`
to decide when a session may terminate.

The selection result is intentionally explicit.  A stale saved device may
fall back to the *current default* only when that default is known to be
usable; the result says that a fallback occurred so a caller cannot present it
as if the requested device had silently been selected.  If no safe default is
available, the result is ``UNAVAILABLE`` rather than an arbitrary device.

The duration and VAD policies are deterministic state machines.  Callers may
provide monotonic timestamps explicitly (which keeps tests and a future audio
callback adapter deterministic), or use the small ``clock`` protocol in the
adapter that owns the recording loop.  This module has no provider, audio
backend, Tk, or secret-storage imports.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import math
import threading
import unicodedata
from typing import Any, Protocol


MICROPHONE_SCHEMA_VERSION = 1
RECORDING_CONTROLS_SCHEMA_VERSION = 1

MAX_MICROPHONE_NAME_CHARS = 256
MAX_HOST_API_CHARS = 128
MAX_NATIVE_ID_CHARS = 512


class MicrophoneConfigurationError(ValueError):
    """A device descriptor or saved microphone setting is invalid."""


class RecordingControlsError(ValueError):
    """A recording boundary setting or observation is invalid."""


class NonMonotonicTimestampError(RecordingControlsError):
    """A policy received a timestamp older than its previous observation."""


def _text(value: Any, *, field_name: str, max_chars: int) -> str:
    value = unicodedata.normalize("NFC", str(value or "")).strip()
    if len(value) > max_chars:
        raise MicrophoneConfigurationError(
            f"{field_name} must contain at most {max_chars} characters")
    return value


def _optional_text(value: Any, *, field_name: str, max_chars: int) -> str:
    if value is None:
        return ""
    return _text(value, field_name=field_name, max_chars=max_chars)


def _flag(value: Any, *, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    # A few backend wrappers expose these flags as 0/1. Do not accept
    # arbitrary truthy strings such as "false", which would mark a missing
    # endpoint as available.
    if value in (0, 1):
        return bool(value)
    raise MicrophoneConfigurationError(f"{field_name} must be boolean")


def _finite_number(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise RecordingControlsError(f"{field_name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise RecordingControlsError(f"{field_name} must be a number") from error
    if not math.isfinite(number):
        raise RecordingControlsError(f"{field_name} must be finite")
    return number


def _duration(
    value: Any,
    *,
    field_name: str,
    allow_none: bool = False,
    minimum: float = 0.0,
) -> float | None:
    if value is None and allow_none:
        return None
    number = _finite_number(value, field_name=field_name)
    if number < minimum:
        comparator = "positive" if minimum > 0 else "non-negative"
        raise RecordingControlsError(f"{field_name} must be {comparator}")
    return number


def _version(
    value: Any,
    *,
    field_name: str,
    supported: int,
    error_type: type[ValueError] = MicrophoneConfigurationError,
) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise error_type(f"{field_name} must be an integer")
    if value < 0 or value > supported:
        raise error_type(
            f"unsupported {field_name} {value}; newest supported version is {supported}")
    return value


def stable_microphone_id(
    name: str,
    host_api: str = "",
    *,
    native_id: str | None = None,
) -> str:
    """Return a deterministic identity that does not depend on enumeration order.

    A native endpoint/GUID is preferred when a backend exposes one.  PortAudio
    does not expose that value consistently on all supported platforms, so the
    safe fallback is the normalized ``host_api + name`` pair.  Device indices
    are deliberately not accepted as identity input: they change after
    hot-plugging and would route a saved preference to an unrelated endpoint.
    """

    normalized_name = _text(name, field_name="microphone name", max_chars=MAX_MICROPHONE_NAME_CHARS)
    if not normalized_name:
        raise MicrophoneConfigurationError("microphone name must not be empty")
    normalized_host = _optional_text(
        host_api, field_name="host API", max_chars=MAX_HOST_API_CHARS)
    normalized_native = _optional_text(
        native_id, field_name="native microphone identity", max_chars=MAX_NATIVE_ID_CHARS)
    source = (
        "native\0" + normalized_host.casefold() + "\0" + normalized_native
        if normalized_native
        else "name\0" + normalized_host.casefold() + "\0" + normalized_name.casefold()
    )
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return f"mic-v1-{digest}"


@dataclass(frozen=True)
class MicrophoneDevice:
    """A normalized input endpoint from one inventory snapshot.

    ``stable_id`` is independent of the transient backend index.  The index
    may still be retained by an adapter outside this module for opening the
    stream, but it must never be persisted as the user's selection.
    """

    stable_id: str
    name: str
    host_api: str = ""
    input_channels: int = 0
    default_sample_rate: float = 0.0
    is_default: bool = False
    available: bool = True
    # This is a current-snapshot handle only. It is intentionally omitted
    # from ``to_mapping`` and never participates in ``stable_id``.
    backend_index: int | None = None

    def __post_init__(self) -> None:
        stable_id = _text(
            self.stable_id, field_name="microphone stable ID", max_chars=MAX_NATIVE_ID_CHARS)
        name = _text(
            self.name, field_name="microphone name", max_chars=MAX_MICROPHONE_NAME_CHARS)
        host_api = _optional_text(
            self.host_api, field_name="host API", max_chars=MAX_HOST_API_CHARS)
        if not stable_id:
            raise MicrophoneConfigurationError("microphone stable ID must not be empty")
        if not name:
            raise MicrophoneConfigurationError("microphone name must not be empty")
        if isinstance(self.input_channels, bool):
            raise MicrophoneConfigurationError(
                "microphone input channel count must be a non-negative integer")
        try:
            channels = int(self.input_channels)
        except (TypeError, ValueError) as error:
            raise MicrophoneConfigurationError(
                "microphone input channel count must be a non-negative integer") from error
        if channels < 0 or (
            isinstance(self.input_channels, float)
            and not self.input_channels.is_integer()
        ):
            raise MicrophoneConfigurationError(
                "microphone input channel count must be a non-negative integer")
        try:
            sample_rate = float(self.default_sample_rate or 0.0)
        except (TypeError, ValueError) as error:
            raise MicrophoneConfigurationError(
                "microphone default sample rate must be numeric") from error
        if not math.isfinite(sample_rate) or sample_rate < 0:
            raise MicrophoneConfigurationError(
                "microphone default sample rate must be finite and non-negative")
        if not isinstance(self.is_default, bool) or not isinstance(self.available, bool):
            raise MicrophoneConfigurationError(
                "microphone availability flags must be boolean")
        backend_index = self.backend_index
        if backend_index is not None:
            if isinstance(backend_index, bool):
                raise MicrophoneConfigurationError(
                    "microphone backend index must be a non-negative integer")
            try:
                backend_index = int(backend_index)
            except (TypeError, ValueError) as error:
                raise MicrophoneConfigurationError(
                    "microphone backend index must be a non-negative integer") from error
            if backend_index < 0 or (
                isinstance(self.backend_index, float)
                and not self.backend_index.is_integer()
            ):
                raise MicrophoneConfigurationError(
                    "microphone backend index must be a non-negative integer")
        object.__setattr__(self, "stable_id", stable_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "host_api", host_api)
        object.__setattr__(self, "input_channels", channels)
        object.__setattr__(self, "default_sample_rate", sample_rate)
        object.__setattr__(self, "backend_index", backend_index)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MicrophoneDevice":
        """Normalize a backend record without treating its index as identity."""

        if not isinstance(value, Mapping):
            raise MicrophoneConfigurationError("microphone device must be an object")
        name = value.get("name", value.get("label", ""))
        host_api = value.get("host_api", value.get("hostapi", ""))
        # A backend-provided GUID/endpoint ID is preferred.  Numeric values
        # from ``sounddevice.default.device`` and ``query_devices`` are
        # indexes, not stable identifiers, and are intentionally ignored.
        native_id = value.get(
            "native_id",
            value.get(
                "guid",
                value.get(
                    "endpoint_id",
                    value.get("device_id", value.get("id", "")),
                ),
            ),
        )
        if isinstance(native_id, (int, float)):
            native_id = ""
        stable_id = value.get("stable_id", "")
        if not isinstance(stable_id, str) or not stable_id.strip():
            stable_id = stable_microphone_id(
                name, host_api, native_id=native_id if native_id else None)
        channels = value.get("input_channels", value.get("max_input_channels", 0))
        sample_rate = value.get(
            "default_sample_rate", value.get("default_samplerate", 0.0))
        backend_index = value.get("backend_index", value.get("index", value.get("device_index")))
        if isinstance(backend_index, bool):
            backend_index = None
        return cls(
            stable_id=stable_id,
            name=name,
            host_api=host_api,
            input_channels=channels,
            default_sample_rate=sample_rate,
            is_default=_flag(
                value.get("is_default"),
                field_name="microphone default flag",
                default=False,
            ),
            available=_flag(
                value.get("available"),
                field_name="microphone availability flag",
                default=True,
            ),
            backend_index=backend_index,
        )

    @property
    def usable(self) -> bool:
        return self.available and self.input_channels > 0

    def to_mapping(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "name": self.name,
            "host_api": self.host_api,
            "input_channels": self.input_channels,
            "default_sample_rate": self.default_sample_rate,
            "is_default": self.is_default,
            "available": self.available,
        }


class MicrophoneSelectionState(str, Enum):
    """What a caller must tell the user before starting capture."""

    SELECTED = "selected"
    DEFAULT = "default"
    FALLBACK_DEFAULT = "fallback_default"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class MicrophoneSelection:
    """Resolved selection and its explicit availability/fallback status."""

    state: MicrophoneSelectionState
    device: MicrophoneDevice | None = None
    requested_id: str | None = None
    reason: str = ""

    @property
    def selected_id(self) -> str | None:
        return self.device.stable_id if self.device is not None else None

    @property
    def can_record(self) -> bool:
        return self.device is not None and self.device.usable

    @property
    def is_fallback(self) -> bool:
        return self.state is MicrophoneSelectionState.FALLBACK_DEFAULT


@dataclass(frozen=True)
class MicrophoneInventory:
    """Immutable snapshot of the input endpoints visible to an adapter."""

    devices: tuple[MicrophoneDevice, ...] = ()
    default_id: str | None = None
    error_code: str = ""

    def __post_init__(self) -> None:
        devices = tuple(self.devices)
        if any(not isinstance(device, MicrophoneDevice) for device in devices):
            raise MicrophoneConfigurationError(
                "microphone inventory entries must be MicrophoneDevice values")
        ids = [device.stable_id for device in devices]
        if len(set(ids)) != len(ids):
            # Two endpoints with the same fallback name/host identity cannot
            # be selected safely without a backend-native ID.
            object.__setattr__(self, "error_code", self.error_code or "ambiguous_identity")
        name_counts: dict[str, int] = {}
        for device in devices:
            if device.usable:
                name = device.name.casefold()
                name_counts[name] = name_counts.get(name, 0) + 1
        ambiguous_names = {
            name for name, count in name_counts.items() if count > 1
        }
        if ambiguous_names:
            # SoX receives only the display name for the selected input. Do
            # not expose endpoints that would resolve to one another when
            # host APIs share that name; they remain visible in the snapshot
            # but are explicitly non-selectable for Settings and Recorder.
            object.__setattr__(
                self, "error_code", self.error_code or "ambiguous_name")
        default_id = self.default_id
        if default_id is not None:
            default_id = str(default_id).strip() or None
        error_code = _text(
            self.error_code, field_name="microphone inventory error", max_chars=64)
        object.__setattr__(self, "devices", devices)
        object.__setattr__(self, "default_id", default_id)
        object.__setattr__(self, "error_code", error_code)

    @classmethod
    def from_records(
        cls,
        records: Iterable[Mapping[str, Any] | MicrophoneDevice],
        *,
        default_id: str | None = None,
        default_index: int | None = None,
    ) -> "MicrophoneInventory":
        """Build a snapshot while translating a transient default index once."""

        devices = tuple(
            record if isinstance(record, MicrophoneDevice)
            else MicrophoneDevice.from_mapping(record)
            for record in records
        )
        resolved_default = str(default_id).strip() if default_id else ""
        default_device_index = None
        if not resolved_default:
            marked = tuple(device.stable_id for device in devices if device.is_default)
            if len(marked) == 1:
                resolved_default = marked[0]
        if not resolved_default and isinstance(default_index, int) and not isinstance(default_index, bool):
            indexed = tuple(
                device for device in devices if device.backend_index == default_index)
            if len(indexed) == 1:
                default_device_index = devices.index(indexed[0])
                resolved_default = indexed[0].stable_id
            elif (
                not indexed
                and all(device.backend_index is None for device in devices)
                and 0 <= default_index < len(devices)
            ):
                # Injected/fake inventories may omit backend handles and use
                # the query order as their only index. Never use this fallback
                # when any real backend index is present, because PortAudio
                # handles may be sparse after filtering.
                default_device_index = default_index
                resolved_default = devices[default_index].stable_id
        if default_device_index is not None:
            devices = tuple(
                replace(device, is_default=True)
                if index == default_device_index else device
                for index, device in enumerate(devices)
            )
        return cls(devices, resolved_default or None)

    @classmethod
    def unavailable(cls, error_code: str = "enumeration_failed") -> "MicrophoneInventory":
        """Represent a backend enumeration failure without raising in the UI."""

        return cls((), None, error_code)

    @property
    def available_devices(self) -> tuple[MicrophoneDevice, ...]:
        ambiguous_names = self._ambiguous_names()
        return tuple(
            device for device in self.devices
            if device.usable and device.name.casefold() not in ambiguous_names
        )

    def _ambiguous_names(self) -> frozenset[str]:
        counts: dict[str, int] = {}
        for device in self.devices:
            if device.usable:
                name = device.name.casefold()
                counts[name] = counts.get(name, 0) + 1
        return frozenset(name for name, count in counts.items() if count > 1)

    def _default_device(self) -> MicrophoneDevice | None:
        if not self.default_id:
            return None
        matches = tuple(
            device for device in self.devices if device.stable_id == self.default_id)
        usable = tuple(device for device in matches if device.usable)
        if not usable:
            return None
        if len(usable) == 1:
            return usable[0]
        marked = tuple(device for device in usable if device.is_default)
        if len(marked) == 1:
            return marked[0]
        # A duplicate fallback ID is unsafe for a stable/name-based request,
        # but resolve(None) is the system-default route and Recorder passes
        # that selection to SoX as -d/default without using this name.
        return usable[0]

    def resolve(self, requested_id: str | None = None) -> MicrophoneSelection:
        """Resolve a saved preference without silently choosing an arbitrary device."""

        requested = str(requested_id).strip() if requested_id else None
        ambiguous_names = self._ambiguous_names()
        matches = tuple(
            device for device in self.devices
            if requested and device.stable_id == requested
        )
        if (
            len(matches) == 1
            and matches[0].usable
            and matches[0].name.casefold() not in ambiguous_names
        ):
            return MicrophoneSelection(
                MicrophoneSelectionState.SELECTED,
                matches[0], requested, "requested_device_available")

        fallback = self._default_device()
        if fallback is not None:
            if requested and (
                len(matches) != 1
                or not matches[0].usable
                or matches[0].name.casefold() in ambiguous_names
            ):
                return MicrophoneSelection(
                    MicrophoneSelectionState.FALLBACK_DEFAULT,
                    fallback,
                    requested,
                    "saved_device_unavailable",
                )
            return MicrophoneSelection(
                MicrophoneSelectionState.DEFAULT,
                fallback,
                requested,
                "current_default",
            )

        reason = "no_input_device" if not requested else "saved_device_unavailable"
        return MicrophoneSelection(
            MicrophoneSelectionState.UNAVAILABLE,
            None,
            requested,
            reason,
        )

    def to_mapping(self) -> dict[str, Any]:
        """Serialize a diagnostic snapshot; callers should not persist it as settings."""

        return {
            "schema_version": MICROPHONE_SCHEMA_VERSION,
            "default_id": self.default_id,
            "devices": [device.to_mapping() for device in self.devices],
            "error_code": self.error_code,
        }


class MicrophoneInventorySource(Protocol):
    def snapshot(self) -> MicrophoneInventory: ...


class SoundDeviceMicrophoneInventory:
    """Small adapter around an injected ``sounddevice``-like module.

    The production application can instantiate this only when it is ready to
    connect Settings/recording to the inventory.  Keeping the module injected
    makes Linux CI and fake hot-plug tests independent from PortAudio.
    """

    def __init__(self, sounddevice_module: Any):
        self._sounddevice = sounddevice_module

    def snapshot(self) -> MicrophoneInventory:
        try:
            raw_records = self._sounddevice.query_devices()
            # A few lightweight wrappers (and test doubles) return the
            # default input mapping for ``query_devices()`` instead of the
            # full sequence. Treat it as one endpoint rather than iterating
            # its dictionary keys into invalid device descriptors.
            if isinstance(raw_records, Mapping):
                one = dict(raw_records)
                one.setdefault("name", "Default input")
                one.setdefault("is_default", True)
                records = (one,)
            else:
                records = tuple(raw_records)
            query_hostapis = getattr(self._sounddevice, "query_hostapis", None)
            try:
                hostapis = tuple(query_hostapis()) if callable(query_hostapis) else ()
            except Exception:
                # Device enumeration remains useful when host-API labels are
                # unavailable; the numeric index is retained as a best-effort
                # fallback rather than hiding every input endpoint.
                hostapis = ()
            if hostapis:
                normalized_records = []
                for record in records:
                    if not isinstance(record, Mapping):
                        normalized_records.append(record)
                        continue
                    raw_host = record.get("host_api", record.get("hostapi", ""))
                    try:
                        host_index = int(raw_host)
                    except (TypeError, ValueError):
                        host_index = -1
                    if (
                        not isinstance(raw_host, bool)
                        and 0 <= host_index < len(hostapis)
                        and isinstance(hostapis[host_index], Mapping)
                    ):
                        host_name = hostapis[host_index].get("name", "")
                        if host_name:
                            record = dict(record)
                            record["host_api"] = host_name
                    normalized_records.append(record)
                records = tuple(normalized_records)
            indexed_records = []
            for index, record in enumerate(records):
                if not isinstance(record, Mapping):
                    indexed_records.append(record)
                    continue
                record = dict(record)
                has_backend_index = any(
                    key in record and record[key] is not None
                    for key in ("backend_index", "index", "device_index")
                )
                if not has_backend_index:
                    # ``query_devices`` returns the complete PortAudio list;
                    # its query position is the backend handle when a fake or
                    # wrapper omitted the explicit index field.
                    record["index"] = index
                indexed_records.append(record)
            records = tuple(indexed_records)
            default = getattr(getattr(self._sounddevice, "default", None), "device", None)
            default_index = None
            if isinstance(default, (tuple, list)):
                default_index = default[0] if default else None
            elif isinstance(default, int) and not isinstance(default, bool):
                default_index = default
            else:
                # sounddevice 0.5.x exposes an _InputOutputPair here. It is
                # pair-like and subscriptable but is not a tuple/list, so do
                # not mistake its shape for an unavailable default.
                try:
                    default_index = default[0]
                except (IndexError, KeyError, TypeError):
                    default_index = getattr(default, "input", None)
            return MicrophoneInventory.from_records(
                records, default_index=default_index)
        except Exception:
            return MicrophoneInventory.unavailable()


@dataclass(frozen=True)
class MicrophoneSettings:
    """Versioned, local-only selected identity for a future Settings boundary."""

    selected_id: str | None = None
    schema_version: int = MICROPHONE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.selected_id is not None and not isinstance(self.selected_id, str):
            raise MicrophoneConfigurationError("selected microphone ID must be text")
        selected = str(self.selected_id).strip() if self.selected_id else None
        if selected == "":
            selected = None
        if selected is not None and len(selected) > MAX_NATIVE_ID_CHARS:
            raise MicrophoneConfigurationError("selected microphone ID is too long")
        object.__setattr__(self, "selected_id", selected)
        object.__setattr__(self, "schema_version", MICROPHONE_SCHEMA_VERSION)

    @classmethod
    def defaults(cls) -> "MicrophoneSettings":
        return cls()

    @classmethod
    def from_mapping(cls, value: Any) -> "MicrophoneSettings":
        if isinstance(value, MicrophoneSettings):
            return value
        if not isinstance(value, Mapping):
            return cls.defaults()
        _version(
            value.get("schema_version"),
            field_name="microphone schema_version",
            supported=MICROPHONE_SCHEMA_VERSION,
        )
        raw = value.get(
            "selected_id",
            value.get("selected_microphone_id", value.get("microphone_id")),
        )
        if raw is not None and not isinstance(raw, str):
            raise MicrophoneConfigurationError("selected microphone ID must be text")
        return cls(raw)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": MICROPHONE_SCHEMA_VERSION,
            "selected_id": self.selected_id,
        }

    def select(self, device: MicrophoneDevice | str | None) -> "MicrophoneSettings":
        if isinstance(device, MicrophoneDevice):
            selected = device.stable_id
        else:
            selected = device
        return MicrophoneSettings(selected)


@dataclass(frozen=True)
class VADSettings:
    """Optional level-based auto-stop settings, disabled by default."""

    enabled: bool = False
    level_threshold: float = 0.02
    minimum_speech_seconds: float = 0.25
    silence_duration_seconds: float = 0.8

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise RecordingControlsError("VAD enabled must be boolean")
        threshold = _finite_number(
            self.level_threshold, field_name="VAD level threshold")
        if threshold < 0 or threshold > 1:
            raise RecordingControlsError(
                "VAD level threshold must be between 0 and 1")
        minimum_speech = _duration(
            self.minimum_speech_seconds,
            field_name="VAD minimum speech duration",
            minimum=0.0,
        )
        silence = _duration(
            self.silence_duration_seconds,
            field_name="VAD silence duration",
            minimum=0.001,
        )
        assert minimum_speech is not None and silence is not None
        object.__setattr__(self, "level_threshold", threshold)
        object.__setattr__(self, "minimum_speech_seconds", minimum_speech)
        object.__setattr__(self, "silence_duration_seconds", silence)

    @classmethod
    def from_mapping(cls, value: Any) -> "VADSettings":
        if isinstance(value, VADSettings):
            return value
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise RecordingControlsError("VAD settings must be an object")
        return cls(
            enabled=value.get("enabled", False),
            level_threshold=value.get(
                "level_threshold", value.get("threshold", 0.02)),
            minimum_speech_seconds=value.get(
                "minimum_speech_seconds", value.get("min_speech_seconds", 0.25)),
            silence_duration_seconds=value.get(
                "silence_duration_seconds", value.get("silence_seconds", 0.8)),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "level_threshold": self.level_threshold,
            "minimum_speech_seconds": self.minimum_speech_seconds,
            "silence_duration_seconds": self.silence_duration_seconds,
        }


@dataclass(frozen=True)
class RecordingControls:
    """Bounded recording settings with behavior-preserving defaults."""

    max_duration_seconds: float | None = None
    warning_seconds: float = 10.0
    vad: VADSettings = field(default_factory=VADSettings)
    schema_version: int = RECORDING_CONTROLS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        maximum = _duration(
            self.max_duration_seconds,
            field_name="maximum recording duration",
            allow_none=True,
            minimum=0.001,
        )
        warning = _duration(
            self.warning_seconds,
            field_name="maximum recording warning",
            minimum=0.0,
        )
        assert warning is not None
        if maximum is not None and warning > maximum:
            raise RecordingControlsError(
                "maximum recording warning cannot exceed maximum duration")
        vad = self.vad if isinstance(self.vad, VADSettings) else VADSettings.from_mapping(self.vad)
        object.__setattr__(self, "max_duration_seconds", maximum)
        object.__setattr__(self, "warning_seconds", warning)
        object.__setattr__(self, "vad", vad)
        object.__setattr__(self, "schema_version", RECORDING_CONTROLS_SCHEMA_VERSION)

    @classmethod
    def defaults(cls) -> "RecordingControls":
        return cls()

    @classmethod
    def from_mapping(cls, value: Any) -> "RecordingControls":
        if isinstance(value, RecordingControls):
            return value
        if not isinstance(value, Mapping):
            return cls.defaults()
        _version(
            value.get("schema_version"),
            field_name="recording controls schema_version",
            supported=RECORDING_CONTROLS_SCHEMA_VERSION,
            error_type=RecordingControlsError,
        )
        vad = value.get("vad", value.get("voice_activity_detection"))
        if vad is None:
            vad = {
                "enabled": value.get("vad_enabled", False),
                "level_threshold": value.get("vad_level_threshold", 0.02),
                "minimum_speech_seconds": value.get("minimum_speech_seconds", 0.25),
                "silence_duration_seconds": value.get("silence_duration_seconds", 0.8),
            }
        return cls(
            max_duration_seconds=value.get(
                "max_duration_seconds", value.get("maximum_duration_seconds")),
            warning_seconds=value.get(
                "warning_seconds", value.get("max_duration_warning_seconds", 10.0)),
            vad=VADSettings.from_mapping(vad),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": RECORDING_CONTROLS_SCHEMA_VERSION,
            "max_duration_seconds": self.max_duration_seconds,
            "warning_seconds": self.warning_seconds,
            "vad": self.vad.to_mapping(),
        }


class RecordingBoundaryReason(str, Enum):
    NONE = "none"
    MAX_DURATION = "max_duration"
    SILENCE = "silence"
    CANCELLED = "cancelled"
    DEVICE_UNAVAILABLE = "device_unavailable"


class RecordingBoundaryState(str, Enum):
    ACTIVE = "active"
    STOPPED = "stopped"
    CANCELLED = "cancelled"
    DEVICE_UNAVAILABLE = "device_unavailable"


@dataclass(frozen=True)
class DurationDecision:
    elapsed_seconds: float
    warning: bool
    should_stop: bool
    reason: RecordingBoundaryReason = RecordingBoundaryReason.NONE


class MaximumDurationPolicy:
    """Pure maximum-duration decision function."""

    def __init__(self, controls: RecordingControls | None = None):
        self.controls = controls or RecordingControls.defaults()

    def evaluate(self, elapsed_seconds: float) -> DurationDecision:
        elapsed = _finite_number(elapsed_seconds, field_name="elapsed recording time")
        if elapsed < 0:
            raise RecordingControlsError("elapsed recording time must be non-negative")
        maximum = self.controls.max_duration_seconds
        if maximum is None:
            return DurationDecision(elapsed, False, False)
        warning_at = max(0.0, maximum - self.controls.warning_seconds)
        warning = elapsed >= warning_at
        expired = elapsed >= maximum
        return DurationDecision(
            elapsed,
            warning,
            expired,
            RecordingBoundaryReason.MAX_DURATION if expired else RecordingBoundaryReason.NONE,
        )


@dataclass(frozen=True)
class VADDecision:
    timestamp: float
    input_level: float
    speech_detected: bool
    speech_seconds: float
    silence_seconds: float
    should_stop: bool
    reason: RecordingBoundaryReason = RecordingBoundaryReason.NONE


class SilenceVADPolicy:
    """Deterministic level-based VAD state machine.

    The policy does not infer speech from a missing sample and never stops
    until it has observed speech for the configured minimum duration followed
    by the configured continuous silence interval.  A caller may feed an
    explicit fake clock by passing ``now`` to :meth:`observe`.
    """

    def __init__(self, settings: VADSettings | None = None):
        self.settings = settings or VADSettings()
        self.reset()

    def reset(self) -> None:
        self._last_timestamp: float | None = None
        self._last_was_speech = False
        self._speech_seen = False
        self._speech_seconds = 0.0
        self._silence_started_at: float | None = None
        self._terminal = False

    @property
    def terminal(self) -> bool:
        return self._terminal

    def start(self, now: float = 0.0) -> None:
        timestamp = _finite_number(now, field_name="VAD start timestamp")
        self.reset()
        self._last_timestamp = timestamp

    def observe(self, now: float, input_level: float) -> VADDecision:
        timestamp = _finite_number(now, field_name="VAD timestamp")
        if self._last_timestamp is not None and timestamp < self._last_timestamp:
            raise NonMonotonicTimestampError(
                "VAD timestamps must be monotonic")
        level = _finite_number(input_level, field_name="VAD input level")
        if level < 0:
            raise RecordingControlsError("VAD input level must be non-negative")
        level = min(1.0, level)
        if self._terminal:
            # Even a terminal policy can receive queued callbacks. Record the
            # latest accepted timestamp so a later callback cannot move time
            # backwards relative to an observation that arrived after the
            # stop decision.
            self._last_timestamp = timestamp
            return VADDecision(
                timestamp,
                level,
                self._last_was_speech,
                self._speech_seconds,
                self._silence_duration(timestamp),
                True,
                RecordingBoundaryReason.SILENCE,
            )

        previous = self._last_timestamp
        if previous is not None and self._last_was_speech:
            self._speech_seconds += timestamp - previous
        speech = self.settings.enabled and level >= self.settings.level_threshold
        if speech:
            self._speech_seen = True
            self._silence_started_at = None
        elif self._speech_seen and self._silence_started_at is None:
            # Silence starts at this observation.  The interval immediately
            # preceding it was classified using the previous sample, so it is
            # not incorrectly counted as silence.
            self._silence_started_at = timestamp

        silence_seconds = self._silence_duration(timestamp)
        if (
            not speech
            and self._speech_seen
            and silence_seconds >= self.settings.silence_duration_seconds
            and self._speech_seconds < self.settings.minimum_speech_seconds
        ):
            # A short noise burst followed by a full silence interval is a
            # discarded utterance, not credit that can be combined with a
            # later utterance to trigger an automatic stop.
            self._speech_seen = False
            self._speech_seconds = 0.0
            self._silence_started_at = None
            silence_seconds = 0.0
        should_stop = bool(
            self.settings.enabled
            and not speech
            and self._speech_seen
            and self._speech_seconds >= self.settings.minimum_speech_seconds
            and silence_seconds >= self.settings.silence_duration_seconds
        )
        if should_stop:
            self._terminal = True
        self._last_timestamp = timestamp
        self._last_was_speech = speech
        return VADDecision(
            timestamp,
            level,
            speech,
            self._speech_seconds,
            silence_seconds,
            should_stop,
            RecordingBoundaryReason.SILENCE if should_stop else RecordingBoundaryReason.NONE,
        )

    def _silence_duration(self, now: float) -> float:
        if self._silence_started_at is None:
            return 0.0
        return max(0.0, now - self._silence_started_at)


@dataclass(frozen=True)
class BoundaryDecision:
    state: RecordingBoundaryState
    reason: RecordingBoundaryReason
    elapsed_seconds: float
    warning: bool = False
    vad: VADDecision | None = None

    @property
    def should_stop(self) -> bool:
        return self.state is not RecordingBoundaryState.ACTIVE


class RecordingBoundaryPolicy:
    """Compose max-duration and optional VAD decisions without side effects."""

    def __init__(
        self,
        controls: RecordingControls | None = None,
        *,
        clock: Any | None = None,
    ):
        self.controls = controls or RecordingControls.defaults()
        self._duration = MaximumDurationPolicy(self.controls)
        self._vad = SilenceVADPolicy(self.controls.vad)
        self._clock = clock
        self._started_at: float | None = None
        self._last_timestamp: float | None = None
        # The duration monitor has its own timestamp sequence.  It may run
        # concurrently with the level callback, so it must not advance the
        # VAD observation clock or make a valid audio callback look stale.
        self._duration_last_timestamp: float | None = None
        self._state = RecordingBoundaryState.ACTIVE
        self._reason = RecordingBoundaryReason.NONE
        self._lock = threading.RLock()

    @property
    def state(self) -> RecordingBoundaryState:
        with self._lock:
            return self._state

    @property
    def terminal(self) -> bool:
        return self.state is not RecordingBoundaryState.ACTIVE

    def _timestamp(self, now: float | None) -> float:
        if now is not None:
            return _finite_number(now, field_name="recording timestamp")
        if self._clock is None:
            raise RecordingControlsError(
                "a timestamp or an injected monotonic clock is required")
        method = getattr(self._clock, "monotonic", None)
        if not callable(method):
            raise RecordingControlsError("recording clock must expose monotonic()")
        return _finite_number(method(), field_name="recording timestamp")

    def start(self, now: float | None = None) -> BoundaryDecision:
        with self._lock:
            timestamp = self._timestamp(now)
            self._started_at = timestamp
            self._last_timestamp = timestamp
            self._duration_last_timestamp = timestamp
            self._state = RecordingBoundaryState.ACTIVE
            self._reason = RecordingBoundaryReason.NONE
            self._vad.start(timestamp)
            return BoundaryDecision(self._state, self._reason, 0.0)

    def observe_duration(self, now: float | None = None) -> BoundaryDecision:
        """Evaluate only the hard duration boundary.

        A source-only recorder may not have level callbacks at all.  Keeping
        this observation separate from :meth:`observe` lets a timer enforce
        maximum duration without feeding synthetic silence into VAD or
        racing the callback's timestamp sequence.
        """
        with self._lock:
            timestamp = self._timestamp(now)
            if (self._duration_last_timestamp is not None
                    and timestamp < self._duration_last_timestamp):
                raise NonMonotonicTimestampError(
                    "recording timestamps must be monotonic")
            if self._started_at is None:
                self._started_at = timestamp
                self._last_timestamp = timestamp
                self._duration_last_timestamp = timestamp
                self._vad.start(timestamp)
            if timestamp < self._started_at:
                raise NonMonotonicTimestampError(
                    "recording timestamps must be monotonic")
            self._duration_last_timestamp = timestamp
            elapsed = timestamp - self._started_at
            if self._state is not RecordingBoundaryState.ACTIVE:
                return BoundaryDecision(self._state, self._reason, elapsed)
            duration = self._duration.evaluate(elapsed)
            if duration.should_stop:
                self._state = RecordingBoundaryState.STOPPED
                self._reason = duration.reason
                return BoundaryDecision(
                    self._state, self._reason, elapsed, duration.warning)
            return BoundaryDecision(
                RecordingBoundaryState.ACTIVE,
                RecordingBoundaryReason.NONE,
                elapsed,
                duration.warning,
            )

    def observe(
        self,
        now: float | None = None,
        *,
        input_level: float = 0.0,
    ) -> BoundaryDecision:
        with self._lock:
            timestamp = self._timestamp(now)
            if self._last_timestamp is not None and timestamp < self._last_timestamp:
                raise NonMonotonicTimestampError(
                    "recording timestamps must be monotonic")
            if self._started_at is None:
                self._started_at = timestamp
                self._vad.start(timestamp)
            if timestamp < self._started_at:
                raise NonMonotonicTimestampError(
                    "recording timestamps must be monotonic")
            elapsed = timestamp - self._started_at
            self._last_timestamp = timestamp
            if self._state is not RecordingBoundaryState.ACTIVE:
                return BoundaryDecision(self._state, self._reason, elapsed)
            duration = self._duration.evaluate(elapsed)
            if duration.should_stop:
                self._state = RecordingBoundaryState.STOPPED
                self._reason = duration.reason
                return BoundaryDecision(
                    self._state, self._reason, elapsed, duration.warning)
            vad = self._vad.observe(timestamp, input_level)
            if vad.should_stop:
                self._state = RecordingBoundaryState.STOPPED
                self._reason = vad.reason
                return BoundaryDecision(
                    self._state, self._reason, elapsed, duration.warning, vad)
            return BoundaryDecision(
                RecordingBoundaryState.ACTIVE,
                RecordingBoundaryReason.NONE,
                elapsed,
                duration.warning,
                vad,
            )

    def cancel(self, now: float | None = None) -> BoundaryDecision:
        return self._terminate(RecordingBoundaryState.CANCELLED, RecordingBoundaryReason.CANCELLED, now)

    def device_lost(self, now: float | None = None) -> BoundaryDecision:
        return self._terminate(
            RecordingBoundaryState.DEVICE_UNAVAILABLE,
            RecordingBoundaryReason.DEVICE_UNAVAILABLE,
            now,
        )

    def _terminate(
        self,
        state: RecordingBoundaryState,
        reason: RecordingBoundaryReason,
        now: float | None,
    ) -> BoundaryDecision:
        with self._lock:
            timestamp = self._timestamp(now)
            if self._started_at is None:
                self._started_at = timestamp
                self._last_timestamp = timestamp
            elif self._last_timestamp is not None and timestamp < self._last_timestamp:
                raise NonMonotonicTimestampError(
                    "recording timestamps must be monotonic")
            if timestamp < self._started_at:
                raise NonMonotonicTimestampError(
                    "recording timestamps must be monotonic")
            self._last_timestamp = timestamp
            if self._state is RecordingBoundaryState.ACTIVE:
                self._state = state
                self._reason = reason
            return BoundaryDecision(
                self._state,
                self._reason,
                timestamp - self._started_at,
            )
