"""Typed, persisted hotkey preferences and activation state.

The desktop application has two intentionally separate hotkey boundaries:

* this module owns the user-facing value (normalisation, validation,
  migration, and the small settings-facing capture API); and
* :mod:`windows_hotkeys` owns the Win32 registration implementation.

Keeping the value object independent of Win32 means settings and workflow
tests can exercise conflict handling on every platform.  A malformed or old
configuration is always normalised to the original Alt shortcuts.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping


HOTKEY_SCHEMA_VERSION = 1


class HotkeyAction(str, Enum):
    """Actions exposed by the global shortcut layer."""

    RECORDING = "recording_hotkey"
    REWRITE = "rewrite_hotkey"
    TRANSLATION = "translation_hotkey"
    VOICE_TRANSLATION = "voice_translation_hotkey"
    VISIBILITY = "toggle_visibility"


class ActivationMode(str, Enum):
    """How the recording shortcut is interpreted."""

    TOGGLE = "toggle"
    PUSH_TO_TALK = "push_to_talk"


SUPPORTED_MODIFIERS = frozenset({"alt", "ctrl", "shift", "win"})
_MODIFIER_ORDER = ("ctrl", "alt", "shift", "win")
_KEY_RE = re.compile(
    r"^(?:[A-Z0-9]|F(?:[1-9]|1[0-9]|2[0-4])|SPACE|TAB|ENTER|ESCAPE|"
    r"UP|DOWN|LEFT|RIGHT)$")


class HotkeyValidationError(ValueError):
    """A setting cannot be represented by the native hotkey layer."""

    def __init__(self, message: str, *, code: str = "invalid_hotkey") -> None:
        super().__init__(message)
        self.code = code


class HotkeyConflictError(HotkeyValidationError):
    """Two ClarifyVoice actions use the same key combination."""

    def __init__(self, conflicts: Mapping[str, tuple[str, ...]]) -> None:
        self.conflicts = dict(conflicts)
        details = "; ".join(
            f"{combo}: {', '.join(actions)}"
            for combo, actions in sorted(self.conflicts.items()))
        super().__init__(
            f"Hotkey conflict between ClarifyVoice actions ({details})",
            code="conflict",
        )


def _normalise_modifiers(value: Any) -> frozenset[str]:
    if isinstance(value, str):
        parts: Iterable[Any] = value.split("+")
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = value
    else:
        parts = ()
    values = (str(part).strip().lower() for part in parts)
    modifiers = frozenset(values)
    aliases = {"control": "ctrl", "windows": "win", "meta": "win"}
    modifiers = frozenset(aliases.get(item, item) for item in modifiers)
    unknown = modifiers - SUPPORTED_MODIFIERS
    if unknown:
        raise HotkeyValidationError(
            f"Unsupported modifier(s): {', '.join(sorted(unknown))}",
            code="unsupported_modifier",
        )
    if not modifiers:
        raise HotkeyValidationError(
            "A global hotkey must include a modifier", code="missing_modifier")
    return modifiers


def _normalise_key(value: Any) -> str:
    key = str(value or "").strip().upper()
    aliases = {"ESC": "ESCAPE", "RETURN": "ENTER", "SPACEBAR": "SPACE"}
    key = aliases.get(key, key)
    # RegisterHotKey supports virtual-key constants.  The four actions only
    # expose printable keys and function keys to keep capture deterministic.
    if not _KEY_RE.fullmatch(key):
        raise HotkeyValidationError(
            f"Unsupported hotkey key: {value!r}", code="unsupported_key")
    return key


@dataclass(frozen=True)
class HotkeyDefinition:
    """One canonical modifier/key pair suitable for RegisterHotKey."""

    modifiers: frozenset[str] = field(default_factory=lambda: frozenset({"alt"}))
    key: str = "L"

    def __post_init__(self) -> None:
        modifiers = _normalise_modifiers(self.modifiers)
        key = _normalise_key(self.key)
        object.__setattr__(self, "modifiers", modifiers)
        object.__setattr__(self, "key", key)

    @classmethod
    def from_mapping(cls, value: Any) -> "HotkeyDefinition":
        if isinstance(value, str):
            parts = [part.strip() for part in value.split("+") if part.strip()]
            if len(parts) < 2:
                raise HotkeyValidationError(
                    "Hotkey text must contain a modifier and a key",
                    code="invalid_format",
                )
            return cls(_normalise_modifiers(parts[:-1]), parts[-1])
        if not isinstance(value, Mapping):
            raise HotkeyValidationError("Hotkey must be an object", code="invalid_format")
        return cls(value.get("modifiers", value.get("mods", ())), value.get("key", ""))

    @classmethod
    def from_event(cls, event: Any) -> "HotkeyDefinition":
        """Capture a Tk/keyboard event without storing platform keycodes."""
        state = int(getattr(event, "state", 0) or 0)
        modifiers = set()
        # Tk's modifier masks are stable across Windows, Linux and macOS for
        # the ordinary keyboard events used by the settings capture field.
        if state & 0x0004:
            modifiers.add("ctrl")
        if state & 0x0001:
            modifiers.add("shift")
        if state & 0x0008:
            modifiers.add("alt")
        if state & 0x0040:
            modifiers.add("win")
        keysym = getattr(event, "keysym", getattr(event, "key", ""))
        return cls(frozenset(modifiers), keysym)

    @property
    def combination(self) -> tuple[frozenset[str], str]:
        return self.modifiers, self.key

    @property
    def display(self) -> str:
        labels = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}
        prefix = "+".join(labels[name] for name in _MODIFIER_ORDER if name in self.modifiers)
        return f"{prefix}+{self.key}"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "modifiers": [name for name in _MODIFIER_ORDER if name in self.modifiers],
            "key": self.key,
        }


DEFAULT_HOTKEYS: dict[HotkeyAction, HotkeyDefinition] = {
    HotkeyAction.RECORDING: HotkeyDefinition(frozenset({"alt"}), "L"),
    HotkeyAction.REWRITE: HotkeyDefinition(frozenset({"alt"}), "K"),
    HotkeyAction.TRANSLATION: HotkeyDefinition(frozenset({"alt"}), "T"),
    HotkeyAction.VOICE_TRANSLATION: HotkeyDefinition(frozenset({"alt"}), "V"),
    HotkeyAction.VISIBILITY: HotkeyDefinition(frozenset({"alt"}), "R"),
}

# The voice-translation binding was added after the original four-action
# configuration.  Keep this migration baseline separate so an old config can
# remain four-action without trying to claim a new global key during startup.
# Fresh defaults still use ``DEFAULT_HOTKEYS`` and therefore include Alt+V.
_LEGACY_DEFAULT_HOTKEYS: dict[HotkeyAction, HotkeyDefinition] = {
    action: definition
    for action, definition in DEFAULT_HOTKEYS.items()
    if action is not HotkeyAction.VOICE_TRANSLATION
}


def _action(value: Any) -> HotkeyAction | None:
    if isinstance(value, HotkeyAction):
        return value
    try:
        return HotkeyAction(str(value))
    except ValueError:
        aliases = {
            "recording": HotkeyAction.RECORDING,
            "rewrite": HotkeyAction.REWRITE,
            "translation": HotkeyAction.TRANSLATION,
            "voice_translation": HotkeyAction.VOICE_TRANSLATION,
            "voice-translation": HotkeyAction.VOICE_TRANSLATION,
            "visibility": HotkeyAction.VISIBILITY,
            "show_hide": HotkeyAction.VISIBILITY,
        }
        return aliases.get(str(value).strip().lower())


def validate_hotkeys(
    values: Mapping[Any, Any],
) -> dict[HotkeyAction, HotkeyDefinition]:
    """Normalise bindings and reject duplicate combinations.

    Omitted actions normally receive their defaults.  The one exception is
    the post-release voice action: it is included only when the input
    explicitly declares it, allowing legacy installations to migrate without
    registering a new global shortcut unexpectedly.
    """
    includes_voice = any(
        _action(raw_action) is HotkeyAction.VOICE_TRANSLATION
        for raw_action in values
    )
    normalised = dict(
        DEFAULT_HOTKEYS if includes_voice else _LEGACY_DEFAULT_HOTKEYS
    )
    for raw_action, raw_definition in values.items():
        action = _action(raw_action)
        if action is None:
            continue
        if isinstance(raw_definition, HotkeyDefinition):
            definition = raw_definition
        else:
            definition = HotkeyDefinition.from_mapping(raw_definition)
        normalised[action] = definition

    by_combination: dict[str, list[str]] = {}
    for action, definition in normalised.items():
        by_combination.setdefault(definition.display, []).append(action.value)
    conflicts = {
        combo: tuple(actions)
        for combo, actions in by_combination.items()
        if len(actions) > 1
    }
    if conflicts:
        raise HotkeyConflictError(conflicts)
    return normalised


@dataclass(frozen=True)
class HotkeySettings:
    """Versioned user preference consumed by repositories and the tray."""

    hotkeys: Mapping[HotkeyAction, HotkeyDefinition] = field(
        default_factory=lambda: dict(DEFAULT_HOTKEYS))
    activation_mode: ActivationMode = ActivationMode.TOGGLE
    schema_version: int = HOTKEY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        normalised = validate_hotkeys(self.hotkeys)
        try:
            mode = (self.activation_mode if isinstance(self.activation_mode, ActivationMode)
                    else ActivationMode(str(self.activation_mode)))
        except ValueError as error:
            raise HotkeyValidationError(
                f"Unsupported recording activation mode: {self.activation_mode!r}",
                code="unsupported_activation_mode",
            ) from error
        object.__setattr__(self, "hotkeys", normalised)
        object.__setattr__(self, "activation_mode", mode)
        object.__setattr__(self, "schema_version", HOTKEY_SCHEMA_VERSION)

    @classmethod
    def defaults(cls) -> "HotkeySettings":
        return cls()

    @classmethod
    def from_mapping(cls, value: Any) -> "HotkeySettings":
        if isinstance(value, HotkeySettings):
            return value
        if not isinstance(value, Mapping):
            return cls.defaults()
        bindings = value.get("bindings", value.get("hotkeys", value))
        parsed: dict[HotkeyAction, Any] = {}
        if isinstance(bindings, Mapping):
            for action, definition in bindings.items():
                if _action(action) is not None:
                    parsed[action] = definition
        # Some pre-release builds used flat keys. Keep this compatibility
        # parser so users never lose their existing Alt shortcuts/settings.
        for action in HotkeyAction:
            for key in (action.value, action.value.removesuffix("_hotkey")):
                if key in value:
                    parsed[action] = value[key]
                    break
        mode = value.get("activation_mode", value.get("recording_activation_mode", "toggle"))
        defaults = (
            DEFAULT_HOTKEYS
            if HotkeyAction.VOICE_TRANSLATION in parsed
            else _LEGACY_DEFAULT_HOTKEYS
        )
        try:
            return cls(parsed or defaults, mode)
        except HotkeyValidationError:
            # One bad entry must not prevent startup. Invalid entries are
            # replaced individually with the migration-safe default.
            repaired: dict[HotkeyAction, HotkeyDefinition] = dict(defaults)
            if isinstance(bindings, Mapping):
                for action, definition in bindings.items():
                    parsed_action = _action(action)
                    if parsed_action is None:
                        continue
                    try:
                        repaired[parsed_action] = HotkeyDefinition.from_mapping(definition)
                    except HotkeyValidationError:
                        continue
            try:
                return cls(repaired, mode)
            except HotkeyValidationError:
                return cls.defaults()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": HOTKEY_SCHEMA_VERSION,
            "activation_mode": self.activation_mode.value,
            "bindings": {
                action.value: definition.to_mapping()
                for action, definition in self.hotkeys.items()
            },
        }

    def definition(self, action: HotkeyAction | str) -> HotkeyDefinition:
        parsed = _action(action)
        if parsed is None:
            raise KeyError(action)
        return self.hotkeys[parsed]

    def with_hotkey(
        self,
        action: HotkeyAction | str,
        definition: HotkeyDefinition | Mapping[str, Any] | str,
    ) -> "HotkeySettings":
        parsed = _action(action)
        if parsed is None:
            raise HotkeyValidationError(f"Unknown hotkey action: {action!r}")
        updated = dict(self.hotkeys)
        updated[parsed] = (definition if isinstance(definition, HotkeyDefinition)
                           else HotkeyDefinition.from_mapping(definition))
        return HotkeySettings(updated, self.activation_mode)

    def reset(self, action: HotkeyAction | str) -> "HotkeySettings":
        parsed = _action(action)
        if parsed is None:
            raise HotkeyValidationError(f"Unknown hotkey action: {action!r}")
        return self.with_hotkey(parsed, DEFAULT_HOTKEYS[parsed])

    def reset_all(self) -> "HotkeySettings":
        return HotkeySettings.defaults()

    def with_activation_mode(
        self,
        mode: ActivationMode | str,
        *,
        push_to_talk_supported: bool = False,
    ) -> "HotkeySettings":
        try:
            parsed = mode if isinstance(mode, ActivationMode) else ActivationMode(str(mode))
        except ValueError as error:
            raise HotkeyValidationError(
                f"Unsupported recording activation mode: {mode!r}",
                code="unsupported_activation_mode",
            ) from error
        if parsed is ActivationMode.PUSH_TO_TALK and not push_to_talk_supported:
            raise HotkeyValidationError(
                "Push-to-talk requires a key-release capable native input layer",
                code="unsupported_activation_mode",
            )
        return HotkeySettings(self.hotkeys, parsed)


class HotkeySettingsController:
    """Small settings-facing capture/reset facade.

    The UI can keep this object as a draft. A candidate is validated against
    every other ClarifyVoice binding before it is returned, so persistence and
    native registration never observe a conflicting intermediate state.
    """

    def __init__(self, settings: HotkeySettings | None = None, *, push_to_talk_supported=False):
        self._settings = settings or HotkeySettings.defaults()
        self.push_to_talk_supported = bool(push_to_talk_supported)
        self._lock = threading.RLock()

    @property
    def settings(self) -> HotkeySettings:
        with self._lock:
            return self._settings

    def capture(self, action: HotkeyAction | str, value: Any) -> HotkeySettings:
        with self._lock:
            candidate = self._settings.with_hotkey(action, value)
            self._settings = candidate
            return candidate

    def capture_event(self, action: HotkeyAction | str, event: Any) -> HotkeySettings:
        """Capture one Tk key event after normalising it to a binding.

        Keeping event parsing here gives the Settings UI a small, deterministic
        seam: invalid keys and conflicts are rejected before the draft changes.
        """
        return self.capture(action, HotkeyDefinition.from_event(event))

    def replace(self, settings: HotkeySettings | Mapping[str, Any]) -> HotkeySettings:
        """Replace the draft when the Settings window restores its baseline."""
        with self._lock:
            self._settings = (
                settings if isinstance(settings, HotkeySettings)
                else HotkeySettings.from_mapping(settings))
            return self._settings

    def reset(self, action: HotkeyAction | str) -> HotkeySettings:
        with self._lock:
            self._settings = self._settings.reset(action)
            return self._settings

    def reset_all(self) -> HotkeySettings:
        with self._lock:
            self._settings = HotkeySettings.defaults()
            return self._settings

    def set_activation_mode(self, mode: ActivationMode | str) -> HotkeySettings:
        with self._lock:
            self._settings = self._settings.with_activation_mode(
                mode, push_to_talk_supported=self.push_to_talk_supported)
            return self._settings


class ActivationRaceState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    ACTIVE = "active"
    STOPPING = "stopping"
    CLOSED = "closed"


class RecordingActivationController:
    """Serialise toggle and push-to-talk edges around async recording start.

    Callbacks are invoked outside the lock. A release/cancel that wins while
    ``on_start`` is in flight is remembered and immediately followed by one
    stop/cancel callback after the start callback returns, preventing both
    double-starts and a recording that remains stuck after a short key press.
    """

    def __init__(
        self,
        on_start: Callable[[], Any],
        on_stop: Callable[[], Any],
        *,
        on_cancel: Callable[[], Any] | None = None,
        mode: ActivationMode = ActivationMode.TOGGLE,
    ) -> None:
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_cancel = on_cancel or on_stop
        self._mode = mode if isinstance(mode, ActivationMode) else ActivationMode(str(mode))
        self._lock = threading.RLock()
        self._state = ActivationRaceState.IDLE
        self._generation = 0
        self._pending_stop = False
        self._closed = False
        self._cancel_notified = False

    @property
    def state(self) -> ActivationRaceState:
        with self._lock:
            return self._state

    @property
    def mode(self) -> ActivationMode:
        return self._mode

    def press(self) -> bool:
        """Handle a key-down edge; return whether it was consumed."""
        with self._lock:
            if self._closed:
                return False
            if self._mode is ActivationMode.TOGGLE and self._state is ActivationRaceState.ACTIVE:
                self._state = ActivationRaceState.STOPPING
                generation = self._generation
                callback = self._on_stop
            elif self._state is ActivationRaceState.IDLE:
                self._state = ActivationRaceState.STARTING
                self._pending_stop = False
                self._generation += 1
                generation = self._generation
                callback = self._on_start
            elif self._state is ActivationRaceState.STARTING:
                self._pending_stop = True
                return True
            else:
                return True
        self._invoke_edge(callback, generation, starting=callback is self._on_start)
        return True

    def release(self) -> bool:
        """Handle a key-up edge; only push-to-talk consumes it as a stop."""
        with self._lock:
            if self._closed or self._mode is not ActivationMode.PUSH_TO_TALK:
                return False
            if self._state is ActivationRaceState.STARTING:
                self._pending_stop = True
                return True
            if self._state is not ActivationRaceState.ACTIVE:
                return False
            self._state = ActivationRaceState.STOPPING
            generation = self._generation
        self._invoke_edge(self._on_stop, generation, starting=False)
        return True

    def cancel(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._state = ActivationRaceState.CLOSED
            self._generation += 1
            self._pending_stop = False
            notify = not self._cancel_notified
            self._cancel_notified = True
        if notify:
            try:
                self._on_cancel()
            except Exception:
                pass

    shutdown = cancel

    def _invoke_edge(self, callback: Callable[[], Any], generation: int, *, starting: bool) -> None:
        try:
            result = callback()
            succeeded = result is not False
        except Exception:
            succeeded = False
        follow_up: Callable[[], Any] | None = None
        with self._lock:
            stale = self._closed or generation != self._generation
            if starting:
                if stale or not succeeded:
                    self._state = (
                        ActivationRaceState.CLOSED
                        if self._closed else ActivationRaceState.IDLE)
                    if succeeded and stale and not self._cancel_notified:
                        self._cancel_notified = True
                        follow_up = self._on_cancel
                elif self._pending_stop:
                    self._pending_stop = False
                    self._state = ActivationRaceState.STOPPING
                    follow_up = self._on_stop
                else:
                    self._state = ActivationRaceState.ACTIVE
            elif not stale:
                self._state = ActivationRaceState.IDLE if succeeded else ActivationRaceState.ACTIVE
        if follow_up is not None:
            try:
                follow_up()
            except Exception:
                pass
        with self._lock:
            if starting and follow_up is self._on_stop and not self._closed:
                self._state = ActivationRaceState.IDLE
            elif not starting and not self._closed and self._state is ActivationRaceState.STOPPING:
                self._state = ActivationRaceState.IDLE if succeeded else ActivationRaceState.ACTIVE


__all__ = [
    "ActivationMode", "ActivationRaceState", "DEFAULT_HOTKEYS", "HOTKEY_SCHEMA_VERSION",
    "HotkeyAction", "HotkeyConflictError", "HotkeyDefinition", "HotkeySettings",
    "HotkeySettingsController", "HotkeyValidationError", "RecordingActivationController",
    "validate_hotkeys",
]
