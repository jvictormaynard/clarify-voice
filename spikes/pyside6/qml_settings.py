"""Typed Qt Quick settings controller for the ClarifyVoice frontend.

The controller keeps an :class:`repositories.AppConfig` draft and exposes
only typed scalar properties and workflow-route mappings to QML.  Persistence
is delegated to ``ApplicationRepositories.config.apply`` so route validation
and atomic storage remain owned by the existing repository boundary.  Windows
autostart is handled by a small, local Run-key boundary.
"""

from __future__ import annotations

import platform
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import Property, QObject, Qt, Signal, Slot

from hotkey_config import (
    ActivationMode,
    HotkeyAction,
    HotkeyDefinition,
    HotkeySettings,
    HotkeySettingsController,
    HotkeyValidationError,
)
from local_asr import PROVIDER_ID as LOCAL_ASR_PROVIDER_ID
from local_asr_product import (
    LocalASRProductController,
    LocalASRProductState,
    format_requirement_bytes,
)
from microphone_controls import (
    MicrophoneInventory,
    MicrophoneSelection,
    MicrophoneSelectionState,
    MicrophoneSettings,
    RecordingControls,
    SoundDeviceMicrophoneInventory,
)
from provider_http import CancellationToken
from provider_registry import PROVIDER_REGISTRY
from provider_types import ProviderCapability, ProviderConnection
from repositories import (
    AppConfig,
    ApplicationRepositories,
    ConfigRepository,
    SUPPORTED_LANGUAGES,
    SUPPORTED_UI_MODES,
)
from workflow_config import (
    WORKFLOW_CAPABILITIES,
    WORKFLOW_SCOPES,
    WorkflowRoute,
    WorkflowScope,
)
from windows_hotkeys import supports_push_to_talk


AUTOSTART_REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE_NAME = "ClarifyVoice"

_HOTKEY_ACTION_LABELS = {
    HotkeyAction.RECORDING: "Record / stop",
    HotkeyAction.REWRITE: "Rewrite selected text",
    HotkeyAction.TRANSLATION: "Translate selected text",
    HotkeyAction.VOICE_TRANSLATION: "Translate voice",
    HotkeyAction.VISIBILITY: "Show / hide window",
}
_HOTKEY_ACTION_ORDER = tuple(_HOTKEY_ACTION_LABELS)


def _hotkey_action(value: object) -> HotkeyAction:
    """Parse the canonical action IDs exposed to QML."""

    if isinstance(value, HotkeyAction):
        return value
    try:
        return HotkeyAction(str(value or "").strip())
    except ValueError as error:
        raise HotkeyValidationError(
            f"Unknown hotkey action: {value!r}", code="unknown_action"
        ) from error


def _qt_enum_value(value: object) -> int:
    """Read either a Qt enum or the integer QML sends through a slot."""

    raw_value = getattr(value, "value", value)
    return int(raw_value)


def _qml_hotkey_definition(
    key_code: object, modifier_flags: object
) -> HotkeyDefinition:
    """Convert a Qt Quick key event into the shared typed hotkey value."""

    key = _qt_enum_value(key_code)
    special_keys = {
        _qt_enum_value(Qt.Key.Key_Space): "SPACE",
        _qt_enum_value(Qt.Key.Key_Tab): "TAB",
        _qt_enum_value(Qt.Key.Key_Enter): "ENTER",
        _qt_enum_value(Qt.Key.Key_Return): "ENTER",
        _qt_enum_value(Qt.Key.Key_Escape): "ESCAPE",
        _qt_enum_value(Qt.Key.Key_Up): "UP",
        _qt_enum_value(Qt.Key.Key_Down): "DOWN",
        _qt_enum_value(Qt.Key.Key_Left): "LEFT",
        _qt_enum_value(Qt.Key.Key_Right): "RIGHT",
    }
    if key in special_keys:
        key_name = special_keys[key]
    elif ord("A") <= key <= ord("Z"):
        key_name = chr(key)
    elif ord("0") <= key <= ord("9"):
        key_name = chr(key)
    else:
        first_function_key = _qt_enum_value(Qt.Key.Key_F1)
        last_function_key = _qt_enum_value(Qt.Key.Key_F24)
        if first_function_key <= key <= last_function_key:
            key_name = f"F{key - first_function_key + 1}"
        else:
            raise HotkeyValidationError(
                "Unsupported hotkey key; choose a letter, number, function key, "
                "or navigation key",
                code="unsupported_key",
            )

    modifiers_value = _qt_enum_value(modifier_flags)
    modifiers = []
    for name, modifier in (
        ("ctrl", Qt.KeyboardModifier.ControlModifier),
        ("alt", Qt.KeyboardModifier.AltModifier),
        ("shift", Qt.KeyboardModifier.ShiftModifier),
        ("win", Qt.KeyboardModifier.MetaModifier),
    ):
        if modifiers_value & _qt_enum_value(modifier):
            modifiers.append(name)
    return HotkeyDefinition(modifiers, key_name)


def _is_windows() -> bool:
    return sys.platform == "win32"


def _registry_module(registry: Any | None = None) -> Any:
    if registry is not None:
        return registry
    import winreg

    return winreg


def _source_qml_entrypoint() -> Path:
    """Return the source-mode QML entrypoint as an absolute path."""

    return Path(__file__).resolve().parent / "qml_app.py"


def _autostart_command(executable: str | None = None) -> str:
    executable = str(executable or sys.executable)
    arguments = [executable]
    if not getattr(sys, "frozen", False):
        arguments.append(str(_source_qml_entrypoint()))
    arguments.append("--hidden")
    return subprocess.list2cmdline(arguments)


def _set_autostart(enabled: bool, registry: Any | None = None) -> None:
    """Create or remove the current user's QML startup entry."""

    if not _is_windows():
        return
    registry = _registry_module(registry)
    with registry.CreateKey(
        registry.HKEY_CURRENT_USER,
        AUTOSTART_REGISTRY_PATH,
    ) as key:
        if enabled:
            registry.SetValueEx(
                key,
                AUTOSTART_VALUE_NAME,
                0,
                registry.REG_SZ,
                _autostart_command(),
            )
        else:
            try:
                registry.DeleteValue(key, AUTOSTART_VALUE_NAME)
            except FileNotFoundError:
                pass


def _autostart_registry_state(
    registry: Any | None = None,
) -> tuple[bool, Any | None, Any | None]:
    """Capture the current Run value, including its Registry type."""

    if not _is_windows():
        return False, None, None
    registry = _registry_module(registry)
    try:
        with registry.OpenKey(
            registry.HKEY_CURRENT_USER,
            AUTOSTART_REGISTRY_PATH,
        ) as key:
            value, kind = registry.QueryValueEx(key, AUTOSTART_VALUE_NAME)
        return True, value, kind
    except OSError:
        return False, None, None


def _restore_autostart_registry_state(
    state: tuple[bool, Any | None, Any | None],
    registry: Any | None = None,
) -> None:
    """Restore a captured Run value without changing its Registry type."""

    if not _is_windows():
        return
    registry = _registry_module(registry)
    exists, value, kind = state
    with registry.CreateKey(
        registry.HKEY_CURRENT_USER,
        AUTOSTART_REGISTRY_PATH,
    ) as key:
        if exists:
            registry.SetValueEx(key, AUTOSTART_VALUE_NAME, 0, kind, value)
        else:
            try:
                registry.DeleteValue(key, AUTOSTART_VALUE_NAME)
            except FileNotFoundError:
                pass


def _apply_config_with_autostart_transaction(
    config: AppConfig,
    repositories: ApplicationRepositories,
    registry: Any | None = None,
) -> AppConfig:
    """Persist QML settings and the Windows Run entry as one operation."""

    if not _is_windows():
        return repositories.config.apply(config)

    previous_registry_state = _autostart_registry_state(registry)
    try:
        _set_autostart(config.startup.autostart, registry)
        return repositories.config.apply(config)
    except Exception:
        try:
            _restore_autostart_registry_state(previous_registry_state, registry)
        except OSError:
            pass
        raise


def _default_local_asr_product() -> LocalASRProductController:
    """Build the Settings-owned lifecycle around the shared local adapter."""

    adapter = PROVIDER_REGISTRY.adapter(LOCAL_ASR_PROVIDER_ID)
    backend = getattr(adapter, "backend", None)
    installer = getattr(backend, "installer", None)
    return LocalASRProductController(installer=installer, backend=backend)


def _format_local_requirements(requirements: dict[str, Any]) -> str:
    """Return a compact, user-facing summary without exposing file paths."""

    download = format_requirement_bytes(requirements.get("download_bytes", 0))
    disk = format_requirement_bytes(requirements.get("disk_bytes", 0))
    memory = format_requirement_bytes(requirements.get("memory_bytes", 0))
    platform = str(requirements.get("platform", "Windows x64"))
    compute = str(requirements.get("compute", "")).strip()
    runtime = str(requirements.get("runtime", "")).strip()
    details = " · ".join(value for value in (compute, runtime) if value)
    suffix = f" · {details}" if details else ""
    return f"{platform} · {download} download · {disk} disk · {memory} RAM{suffix}"


def _default_microphone_inventory_source() -> Any | None:
    """Build the optional PortAudio inventory without making it a hard import."""

    try:
        import sounddevice
    except (ImportError, OSError):
        return None
    return SoundDeviceMicrophoneInventory(sounddevice)


class QmlSettingsController(QObject):
    """Expose the typed application configuration to Qt Quick.

    The object owns an editable draft only.  ``load`` replaces that draft
    from the configured repository, while ``save`` delegates the complete
    ``AppConfig`` to the repository's atomic ``apply`` operation.  No legacy
    window or frontend is involved; Windows startup is handled by the local
    Run-key transaction only when settings are saved.
    """

    configChanged = Signal()
    routeChanged = Signal()
    selectedScopeChanged = Signal()
    dirtyChanged = Signal()
    errorChanged = Signal()
    loaded = Signal()
    saved = Signal()
    providerStateChanged = Signal()
    microphoneChanged = Signal()
    microphoneTestChanged = Signal()
    hotkeyChanged = Signal()
    _providerValidationFinished = Signal(str, int, bool, object)
    _localStatePublished = Signal(object)
    _microphoneTestFinished = Signal(int, bool, object)

    def __init__(
        self,
        repositories: ApplicationRepositories,
        parent: QObject | None = None,
        *,
        registry: Any | None = None,
        local_product: LocalASRProductController | None = None,
        microphone_backend: Any | None = None,
        microphone_inventory_source: Any | None = None,
        hotkey_applier: Callable[[HotkeySettings], Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self.repositories = repositories
        self._config_repository: ConfigRepository = repositories.config
        self._autostart_registry = registry
        self._local_product = local_product or _default_local_asr_product()
        self._local_state: LocalASRProductState = self._local_product.state
        self._hotkey_applier = hotkey_applier
        self._microphone_backend = microphone_backend
        self._microphone_inventory_source = (
            microphone_inventory_source
            or getattr(microphone_backend, "microphone_inventory_source", None)
            or _default_microphone_inventory_source()
        )
        self._microphone_inventory = MicrophoneInventory.unavailable("not_refreshed")
        self._microphone_test_generation = 0
        self._microphone_test_busy = False
        self._microphone_test_status = ""
        self._microphone_test_kind = "info"
        self._provider_activity: dict[str, dict[str, Any]] = {}
        self._provider_tokens: dict[str, CancellationToken] = {}
        self._provider_generations: dict[str, int] = {}
        self._selected_scope = WorkflowScope.TRANSCRIPTION.value
        self._config = self._config_repository.load()
        configured_provider = (
            str(self._config.selection.transcription_provider or "gemini")
            .strip()
            .lower()
        )
        self._selected_provider_id = (
            configured_provider
            if configured_provider in PROVIDER_REGISTRY.provider_ids
            else PROVIDER_REGISTRY.provider_ids[0]
        )
        self._hotkey_settings_controller = HotkeySettingsController(
            self._config.hotkeys,
            push_to_talk_supported=supports_push_to_talk(),
        )
        self._hotkey_capture_action = ""
        self._provider_api_key_draft = ""
        self._provider_base_url_draft = self._config_provider_base_url(
            self._selected_provider_id
        )
        self._dirty = False
        self._last_error = ""
        self._providerValidationFinished.connect(
            self._finish_provider_validation,
            Qt.ConnectionType.QueuedConnection,
        )
        self._localStatePublished.connect(
            self._apply_local_state,
            Qt.ConnectionType.QueuedConnection,
        )
        self._microphoneTestFinished.connect(
            self._finish_microphone_test,
            Qt.ConnectionType.QueuedConnection,
        )
        self._local_product.subscribe(self._publish_local_state)
        self._local_product.refresh_async()
        self.refreshMicrophoneInventory()

    @Property("QStringList", constant=True)
    def workflowScopes(self) -> list[str]:
        """Canonical route identifiers available to the settings form."""

        return list(WORKFLOW_SCOPES)

    @Property("QStringList", constant=True)
    def modes(self) -> list[str]:
        return list(SUPPORTED_UI_MODES)

    @Property("QStringList", constant=True)
    def languages(self) -> list[str]:
        return list(SUPPORTED_LANGUAGES)

    @Property("QStringList", constant=True)
    def providerIds(self) -> list[str]:
        return list(PROVIDER_REGISTRY.provider_ids)

    @Property("QVariantList", notify=hotkeyChanged)
    def hotkeyActions(self) -> list[dict[str, Any]]:
        """Return the complete, ordered hotkey catalog for the QML form."""

        return [self._hotkey_mapping(action) for action in _HOTKEY_ACTION_ORDER]

    @Property("QVariantMap", notify=hotkeyChanged)
    def hotkeyDefinitions(self) -> dict[str, dict[str, Any]]:
        """Expose canonical definitions without moving validation into QML."""

        return {
            action.value: self._hotkey_mapping(action)
            for action in _HOTKEY_ACTION_ORDER
        }

    @Property("QStringList", constant=True)
    def hotkeyActivationModes(self) -> list[str]:
        return [mode.value for mode in ActivationMode]

    @Property(str, notify=hotkeyChanged)
    def hotkeyActivationMode(self) -> str:
        return self._hotkey_settings_controller.settings.activation_mode.value

    @Property(bool, constant=True)
    def hotkeyPushToTalkSupported(self) -> bool:
        return self._hotkey_settings_controller.push_to_talk_supported

    @Property(str, notify=hotkeyChanged)
    def hotkeyCaptureAction(self) -> str:
        return self._hotkey_capture_action

    @Property("QVariantMap", notify=providerStateChanged)
    def providerStates(self) -> dict[str, dict[str, Any]]:
        return {
            provider_id: self._provider_state(provider_id)
            for provider_id in PROVIDER_REGISTRY.provider_ids
        }

    @Property(str, notify=providerStateChanged)
    def selectedProviderId(self) -> str:
        return self._selected_provider_id

    @Property(str, notify=providerStateChanged)
    def providerDisplayName(self) -> str:
        return PROVIDER_REGISTRY.describe(self._selected_provider_id).display_name

    @Property(str, notify=providerStateChanged)
    def providerApiKey(self) -> str:
        """Return only the editable key draft, never the stored secret."""

        return self._provider_api_key_draft

    @providerApiKey.setter
    def providerApiKey(self, value: str) -> None:
        self.setProviderApiKey(value)

    @Property(str, notify=providerStateChanged)
    def providerBaseUrl(self) -> str:
        return self._provider_base_url_draft

    @providerBaseUrl.setter
    def providerBaseUrl(self, value: str) -> None:
        self.setProviderBaseUrl(value)

    @Property(bool, notify=providerStateChanged)
    def providerHasApiKey(self) -> bool:
        return bool(
            self._provider_api_key_draft.strip()
            or self._provider_config(self._selected_provider_id).api_key.strip()
        )

    @Property(bool, notify=providerStateChanged)
    def providerSupportsCustomEndpoint(self) -> bool:
        return PROVIDER_REGISTRY.supports(
            self._selected_provider_id,
            ProviderCapability.CUSTOM_BASE_URL,
        )

    @Property(str, notify=providerStateChanged)
    def providerStatus(self) -> str:
        return str(self._provider_state(self._selected_provider_id)["status"])

    @Property(str, notify=providerStateChanged)
    def providerError(self) -> str:
        return str(self._provider_state(self._selected_provider_id).get("error", ""))

    @Property(bool, notify=providerStateChanged)
    def providerBusy(self) -> bool:
        return bool(self._provider_state(self._selected_provider_id).get("busy"))

    @Property(str, notify=providerStateChanged)
    def localAsrStatus(self) -> str:
        return self._local_state.status

    @Property(str, notify=providerStateChanged)
    def localAsrDetail(self) -> str:
        return self._local_state.detail

    @Property(str, notify=providerStateChanged)
    def localAsrRequirements(self) -> str:
        return _format_local_requirements(self._local_state.requirements or {})

    @Property(float, notify=providerStateChanged)
    def localAsrProgress(self) -> float:
        return self._local_state.fraction

    @Property(bool, notify=providerStateChanged)
    def localAsrBusy(self) -> bool:
        return self._local_product.busy

    @Property(str, notify=configChanged)
    def mode(self) -> str:
        return self._config.ui.mode

    @mode.setter
    def mode(self, value: str) -> None:
        self.setMode(value)

    @Property(str, notify=configChanged)
    def language(self) -> str:
        return self._config.ui.language

    @language.setter
    def language(self, value: str) -> None:
        self.setLanguage(value)

    @Property(bool, notify=configChanged)
    def autostart(self) -> bool:
        return self._config.startup.autostart

    @autostart.setter
    def autostart(self, value: bool) -> None:
        self.setAutostart(value)

    @Property(bool, notify=configChanged)
    def historyEnabled(self) -> bool:
        return self._config.history_enabled

    @historyEnabled.setter
    def historyEnabled(self, value: bool) -> None:
        self.setHistoryEnabled(value)

    @Property("QVariant", notify=configChanged)
    def historyRetentionDays(self) -> int | None:
        return self._config.history_retention_days

    @historyRetentionDays.setter
    def historyRetentionDays(self, value: object) -> None:
        self.setHistoryRetentionDays(value)

    @Property("QVariant", notify=microphoneChanged)
    def selectedMicrophoneId(self) -> str | None:
        """Return the draft's stable microphone identity, if one is set."""

        return self._config.microphone.selected_id

    @Property("QVariantList", notify=microphoneChanged)
    def microphoneDevices(self) -> list[dict[str, Any]]:
        """Return a QML-friendly default option plus safe input endpoints."""

        options: list[dict[str, Any]] = [
            {
                "id": "",
                "label": "System default",
                "name": "System default",
                "hostApi": "",
                "isDefault": True,
            }
        ]
        for device in self._microphone_selectable_devices():
            label = device.name
            if device.host_api:
                label = f"{label} · {device.host_api}"
            if any(option["label"] == label for option in options):
                label = f"{label} · {device.stable_id[-6:]}"
            options.append(
                {
                    "id": device.stable_id,
                    "label": label,
                    "name": device.name,
                    "hostApi": device.host_api,
                    "isDefault": bool(device.is_default),
                }
            )
        return options

    @Property(int, notify=microphoneChanged)
    def microphoneSelectionIndex(self) -> int:
        selected = self._config.microphone.selected_id
        if selected:
            for index, option in enumerate(self.microphoneDevices):
                if option.get("id") == selected:
                    return index
        # A stale or backend-unsafe identity is deliberately shown as the
        # recoverable System default option until the user chooses a new one.
        return 0

    @Property("QVariantMap", notify=microphoneChanged)
    def microphoneInventory(self) -> dict[str, Any]:
        return self._microphone_inventory.to_mapping()

    @Property(str, notify=microphoneChanged)
    def microphoneSelectionState(self) -> str:
        return self._microphone_selection().state.value

    @Property(str, notify=microphoneChanged)
    def microphoneStatus(self) -> str:
        return self._microphone_status()[0]

    @Property(str, notify=microphoneChanged)
    def microphoneStatusKind(self) -> str:
        return self._microphone_status()[1]

    @Property(str, notify=microphoneTestChanged)
    def microphoneTestStatus(self) -> str:
        return self._microphone_test_status

    @Property(str, notify=microphoneTestChanged)
    def microphoneTestStatusKind(self) -> str:
        return self._microphone_test_kind

    @Property(bool, notify=microphoneTestChanged)
    def microphoneTestBusy(self) -> bool:
        return self._microphone_test_busy

    @Property("QVariantMap", notify=microphoneChanged)
    def recordingControls(self) -> dict[str, Any]:
        """Expose the typed boundary settings without recreating their rules."""

        return self._config.recording_controls.to_mapping()

    @Property(bool, notify=configChanged)
    def localAsrCloudRefinement(self) -> bool:
        return self._config.local_asr_cloud_refinement

    @localAsrCloudRefinement.setter
    def localAsrCloudRefinement(self, value: bool) -> None:
        self.setLocalAsrCloudRefinement(value)

    @Property(str, notify=selectedScopeChanged)
    def selectedScope(self) -> str:
        return self._selected_scope

    @selectedScope.setter
    def selectedScope(self, value: str) -> None:
        self.selectWorkflow(value)

    @Property(str, notify=routeChanged)
    def routeProviderId(self) -> str:
        return self._selected_route().provider_id

    @routeProviderId.setter
    def routeProviderId(self, value: str) -> None:
        self.setRouteProviderId(value)

    @Property(str, notify=routeChanged)
    def routeModelId(self) -> str:
        return self._selected_route().model_id

    @routeModelId.setter
    def routeModelId(self, value: str) -> None:
        self.setRouteModelId(value)

    @Property(str, notify=routeChanged)
    def routePrompt(self) -> str:
        return self._selected_route().prompt

    @routePrompt.setter
    def routePrompt(self, value: str) -> None:
        self.setRoutePrompt(value)

    @Property(str, notify=routeChanged)
    def routeCustomEndpoint(self) -> str:
        return self._selected_route().custom_endpoint

    @routeCustomEndpoint.setter
    def routeCustomEndpoint(self, value: str) -> None:
        self.setRouteCustomEndpoint(value)

    @Property(bool, notify=routeChanged)
    def routeEnabled(self) -> bool:
        return self._selected_route().enabled

    @routeEnabled.setter
    def routeEnabled(self, value: bool) -> None:
        self.setRouteEnabled(value)

    @Property("QVariantMap", notify=configChanged)
    def routes(self) -> dict[str, dict[str, Any]]:
        """Return all typed routes in a QML-friendly, non-secret mapping."""

        return {
            scope: self._route_mapping(self._config.workflow(scope), scope)
            for scope in WORKFLOW_SCOPES
        }

    @Property(bool, notify=dirtyChanged)
    def dirty(self) -> bool:
        return self._dirty

    @Property(str, notify=errorChanged)
    def lastError(self) -> str:
        return self._last_error

    @Slot(str, result=bool)
    def selectWorkflow(self, scope: str) -> bool:
        try:
            normalized = self._canonical_scope(scope)
        except ValueError as error:
            self._set_error(error)
            return False
        self._set_error(None)
        if normalized == self._selected_scope:
            return True
        self._selected_scope = normalized
        self.selectedScopeChanged.emit()
        self.routeChanged.emit()
        return True

    @Slot(str, result=bool)
    def setMode(self, value: str) -> bool:
        try:
            normalized = self._choice(value, SUPPORTED_UI_MODES, "mode")
        except ValueError as error:
            self._set_error(error)
            return False
        return self._update_config(
            replace(self._config, ui=replace(self._config.ui, mode=normalized))
        )

    @Slot(str, result=bool)
    def setLanguage(self, value: str) -> bool:
        try:
            normalized = self._choice(value, SUPPORTED_LANGUAGES, "language")
        except ValueError as error:
            self._set_error(error)
            return False
        return self._update_config(
            replace(
                self._config,
                ui=replace(self._config.ui, language=normalized),
            )
        )

    @Slot(bool, result=bool)
    def setAutostart(self, value: bool) -> bool:
        return self._update_config(
            replace(
                self._config,
                startup=replace(self._config.startup, autostart=bool(value)),
            )
        )

    @Slot(bool, result=bool)
    def setHistoryEnabled(self, value: bool) -> bool:
        return self._update_config(replace(self._config, history_enabled=bool(value)))

    @Slot(object, result=bool)
    def setHistoryRetentionDays(self, value: object) -> bool:
        try:
            if value is not None:
                if isinstance(value, bool):
                    raise ValueError("history retention must be an integer")
                if isinstance(value, float) and not value.is_integer():
                    raise ValueError("history retention must be an integer")
                value = int(value)
                if value < 0:
                    raise ValueError("history retention cannot be negative")
            retention = cast(int | None, value)
        except (TypeError, ValueError) as error:
            self._set_error(error)
            return False
        return self._update_config(
            replace(self._config, history_retention_days=retention)
        )

    @Slot(str, result=bool)
    def beginHotkeyCapture(self, action: str) -> bool:
        """Put one QML row into capture mode without changing the draft."""

        try:
            parsed = _hotkey_action(action)
        except HotkeyValidationError as error:
            self._set_error(error)
            return False
        self._set_error(None)
        next_action = parsed.value
        if next_action != self._hotkey_capture_action:
            self._hotkey_capture_action = next_action
            self.hotkeyChanged.emit()
        return True

    @Slot(result=bool)
    def cancelHotkeyCapture(self) -> bool:
        """Leave capture mode without changing the current draft."""

        if self._hotkey_capture_action:
            self._hotkey_capture_action = ""
            self.hotkeyChanged.emit()
        return True

    @Slot(str, object, result=bool)
    def setHotkey(self, action: str, definition: object) -> bool:
        """Validate and stage one canonical hotkey definition."""

        try:
            parsed = _hotkey_action(action)
            settings = self._hotkey_settings_controller.capture(parsed, definition)
        except (HotkeyValidationError, TypeError, ValueError) as error:
            self._set_error(error)
            return False
        return self._update_config(replace(self._config, hotkeys=settings))

    @Slot(str, int, int, result=bool)
    def captureHotkey(
        self,
        action: str,
        key_code: int,
        modifier_flags: int,
    ) -> bool:
        """Capture a Qt Quick key event through the typed hotkey boundary."""

        try:
            definition = _qml_hotkey_definition(key_code, modifier_flags)
        except (TypeError, ValueError, HotkeyValidationError) as error:
            self._set_error(error)
            return False
        return self.setHotkey(action, definition)

    @Slot(str, result=bool)
    def resetHotkey(self, action: str) -> bool:
        """Restore one action to its shared default definition."""

        try:
            parsed = _hotkey_action(action)
            settings = self._hotkey_settings_controller.reset(parsed)
        except (HotkeyValidationError, TypeError, ValueError) as error:
            self._set_error(error)
            return False
        self.cancelHotkeyCapture()
        return self._update_config(replace(self._config, hotkeys=settings))

    @Slot(result=bool)
    def resetAllHotkeys(self) -> bool:
        """Restore every action and the recording activation mode."""

        settings = self._hotkey_settings_controller.reset_all()
        self.cancelHotkeyCapture()
        return self._update_config(replace(self._config, hotkeys=settings))

    @Slot(str, result=bool)
    def setHotkeyActivationMode(self, mode: str) -> bool:
        """Stage the recording activation mode using shared validation."""

        try:
            settings = self._hotkey_settings_controller.set_activation_mode(mode)
        except (HotkeyValidationError, TypeError, ValueError) as error:
            self._set_error(error)
            return False
        return self._update_config(replace(self._config, hotkeys=settings))

    @Slot(object, result=bool)
    def setMicrophone(self, value: object) -> bool:
        """Update only the stable microphone preference in the draft."""

        if value is not None and not isinstance(value, str):
            self._set_error(ValueError("microphone ID must be text"))
            return False
        selected_id = str(value or "").strip() or None
        if selected_id and not self._microphone_supports_explicit_selection():
            self._set_error(
                ValueError(
                    "Explicit microphone selection is unavailable with SoX PulseAudio"
                )
            )
            return False
        try:
            microphone = MicrophoneSettings(selected_id)
        except (TypeError, ValueError) as error:
            self._set_error(error)
            return False
        return self._update_config(replace(self._config, microphone=microphone))

    @Slot(object, result=bool)
    def selectMicrophone(self, value: object) -> bool:
        """QML-friendly alias for the explicit selection action."""

        return self.setMicrophone(value)

    @Slot(object, result=bool)
    def setRecordingControls(self, value: object) -> bool:
        """Validate boundary settings through the shared typed policy."""

        try:
            controls = RecordingControls.from_mapping(value)
        except (TypeError, ValueError) as error:
            self._set_error(error)
            return False
        return self._update_config(replace(self._config, recording_controls=controls))

    @Slot(result=bool)
    def refreshMicrophoneInventory(self) -> bool:
        """Refresh one inventory snapshot without changing the saved draft."""

        try:
            inventory = self._snapshot_microphone_inventory()
        except Exception as error:
            inventory = MicrophoneInventory.unavailable("enumeration_failed")
            self._set_error(error)
        else:
            # Hardware enumeration is diagnostic state, not an editable
            # configuration error.  Leave an unrelated Settings draft error
            # untouched only when the snapshot itself was successful.
            self._set_error(None)
        self._microphone_inventory = inventory
        self.microphoneChanged.emit()
        return True

    @Slot(result=bool)
    def refreshMicrophones(self) -> bool:
        """Short alias retained for a concise QML action name."""

        return self.refreshMicrophoneInventory()

    @Slot(result=bool)
    def testMicrophone(self) -> bool:
        """Run a short local-only input-level test off the Qt GUI thread."""

        if self._microphone_test_busy:
            return False
        selection = self._microphone_selection_for_test()
        if selection is None or not selection.can_record:
            self._microphone_test_status = "No safe microphone to test."
            self._microphone_test_kind = "error"
            self.microphoneTestChanged.emit()
            return False

        backend_test = getattr(self._microphone_backend, "test_microphone", None)
        if not callable(backend_test):
            self._microphone_test_status = "Input test unavailable without PortAudio."
            self._microphone_test_kind = "info"
            self.microphoneTestChanged.emit()
            return False

        self._microphone_test_generation += 1
        generation = self._microphone_test_generation
        self._microphone_test_busy = True
        self._microphone_test_status = "Listening…"
        self._microphone_test_kind = "info"
        self.microphoneTestChanged.emit()
        inventory = self._microphone_inventory

        def run() -> None:
            try:
                peak = float(backend_test(selection, inventory))
            except Exception as error:
                self._microphoneTestFinished.emit(
                    generation,
                    False,
                    str(error).strip() or type(error).__name__,
                )
                return
            self._microphoneTestFinished.emit(generation, True, peak)

        threading.Thread(
            target=run,
            name="ClarifyVoiceQmlMicrophoneTest",
            daemon=True,
        ).start()
        return True

    @Slot(str, str, str, str, str, bool, result=bool)
    def setRoute(
        self,
        scope: str,
        provider_id: str,
        model_id: str,
        prompt: str,
        custom_endpoint: str,
        enabled: bool,
    ) -> bool:
        """Replace one route draft using QML form values."""

        try:
            normalized = self._canonical_scope(scope)
        except ValueError as error:
            self._set_error(error)
            return False
        current = self._config.workflow(normalized)
        route = replace(
            current,
            provider_id=str(provider_id or "").strip().lower(),
            model_id=str(model_id or "").strip(),
            prompt=str(prompt or "").strip(),
            custom_endpoint=str(custom_endpoint or "").strip(),
            enabled=bool(enabled),
            independent=True,
        )
        return self._update_config(
            replace(
                self._config,
                workflows=self._config.workflows.with_route(normalized, route),
            )
        )

    @Slot(str, result=bool)
    def setRouteProviderId(self, value: str) -> bool:
        return self._update_selected_route(
            lambda route: replace(route, provider_id=str(value or "").strip().lower())
        )

    @Slot(str, result=bool)
    def setRouteModelId(self, value: str) -> bool:
        return self._update_selected_route(
            lambda route: replace(route, model_id=str(value or "").strip())
        )

    @Slot(str, result=bool)
    def setRoutePrompt(self, value: str) -> bool:
        return self._update_selected_route(
            lambda route: replace(route, prompt=str(value or "").strip())
        )

    @Slot(str, result=bool)
    def setRouteCustomEndpoint(self, value: str) -> bool:
        return self._update_selected_route(
            lambda route: replace(route, custom_endpoint=str(value or "").strip())
        )

    @Slot(bool, result=bool)
    def setRouteEnabled(self, value: bool) -> bool:
        return self._update_selected_route(
            lambda route: replace(route, enabled=bool(value))
        )

    @Slot(str, result="QVariantMap")
    def routeFor(self, scope: str) -> dict[str, Any]:
        try:
            normalized = self._canonical_scope(scope)
        except ValueError as error:
            self._set_error(error)
            return {}
        self._set_error(None)
        return self._route_mapping(self._config.workflow(normalized), normalized)

    @Slot(str, result="QStringList")
    def providersForScope(self, scope: str) -> list[str]:
        try:
            normalized = self._canonical_scope(scope)
        except ValueError as error:
            self._set_error(error)
            return []
        self._set_error(None)
        capability = WORKFLOW_CAPABILITIES[normalized]
        return [
            metadata.provider_id
            for metadata in PROVIDER_REGISTRY.metadata
            if metadata.supports(capability)
        ]

    @Slot(str, result=bool)
    def selectProvider(self, provider_id: str) -> bool:
        normalized = str(provider_id or "").strip().lower()
        if normalized not in PROVIDER_REGISTRY.provider_ids:
            self._set_error(ValueError(f"Unknown provider: {normalized or '<empty>'}"))
            return False
        if normalized == self._selected_provider_id:
            return True
        self._selected_provider_id = normalized
        self._provider_api_key_draft = ""
        self._provider_base_url_draft = self._config_provider_base_url(normalized)
        self._set_error(None)
        self.providerStateChanged.emit()
        return True

    @Slot(str, result=bool)
    def setProviderApiKey(self, value: str) -> bool:
        self._provider_api_key_draft = str(value or "")
        self._set_error(None)
        self.providerStateChanged.emit()
        return True

    @Slot(str, result=bool)
    def setProviderBaseUrl(self, value: str) -> bool:
        self._provider_base_url_draft = str(value or "").strip()
        self._set_error(None)
        self.providerStateChanged.emit()
        return True

    @Slot(result=bool)
    def validateProvider(self) -> bool:
        """Validate and persist the selected cloud provider without blocking QML."""

        provider_id = self._selected_provider_id
        if provider_id == LOCAL_ASR_PROVIDER_ID:
            self._set_error(
                ValueError("Local Whisper is configured with the install action")
            )
            return False
        if self.providerBusy:
            return False

        provider_config = self._provider_config(provider_id)
        api_key = (
            self._provider_api_key_draft.strip() or provider_config.api_key.strip()
        )
        if not api_key:
            self._set_error(
                ValueError("Enter an API key before validating the provider")
            )
            return False
        metadata = PROVIDER_REGISTRY.describe(provider_id)
        base_url = self._provider_base_url_draft.strip() or metadata.default_base_url
        if not metadata.supports(ProviderCapability.CUSTOM_BASE_URL):
            base_url = metadata.default_base_url
        token = CancellationToken()
        generation = self._provider_generations.get(provider_id, 0) + 1
        self._provider_generations[provider_id] = generation
        previous = self._provider_tokens.get(provider_id)
        if previous is not None:
            previous.cancel()
        self._provider_tokens[provider_id] = token
        self._provider_activity[provider_id] = {
            "status": "validating",
            "error": "",
            "models": [],
            "textModels": [],
            "busy": True,
        }
        self._set_error(None)
        self.providerStateChanged.emit()

        def validate() -> None:
            try:
                catalog = PROVIDER_REGISTRY.discover_models(
                    provider_id,
                    ProviderConnection(api_key, base_url),
                    token,
                )
            except Exception as error:
                self._providerValidationFinished.emit(
                    provider_id,
                    generation,
                    False,
                    {"error": str(error).strip() or type(error).__name__},
                )
                return
            self._providerValidationFinished.emit(
                provider_id,
                generation,
                True,
                {
                    "api_key": api_key,
                    "base_url": base_url,
                    "models": list(catalog.audio_models),
                    "text_models": list(catalog.text_models),
                },
            )

        threading.Thread(
            target=validate,
            name=f"ClarifyVoiceProviderValidation-{provider_id}",
            daemon=True,
        ).start()
        return True

    @Slot(result=bool)
    def clearProvider(self) -> bool:
        """Remove the selected cloud credential from the secure store."""

        provider_id = self._selected_provider_id
        if provider_id == LOCAL_ASR_PROVIDER_ID:
            return False
        generation = self._provider_generations.get(provider_id, 0) + 1
        self._provider_generations[provider_id] = generation
        pending = self._provider_tokens.pop(provider_id, None)
        if pending is not None:
            pending.cancel()
        try:
            persisted = self._config_repository.load()
            persisted_provider = getattr(persisted, provider_id)
            self._persist_provider_credentials(
                provider_id,
                "",
                persisted_provider.base_url,
                persisted_config=persisted,
            )
        except Exception as error:
            message = str(error).strip() or type(error).__name__
            self._provider_activity[provider_id] = {
                "status": "error",
                "error": message,
                "models": [],
                "textModels": [],
                "busy": False,
            }
            self._set_error(error)
            self.providerStateChanged.emit()
            return False
        self._provider_activity[provider_id] = {
            "status": "not_configured",
            "error": "",
            "models": [],
            "textModels": [],
            "busy": False,
        }
        self._provider_api_key_draft = ""
        self._set_error(None)
        self.configChanged.emit()
        self.providerStateChanged.emit()
        return True

    @Slot(result=bool)
    def installLocalAsr(self) -> bool:
        try:
            self._local_product.install_async()
        except Exception as error:
            self._set_error(error)
            self.providerStateChanged.emit()
            return False
        self._set_error(None)
        self.providerStateChanged.emit()
        return True

    @Slot(result=bool)
    def removeLocalAsr(self) -> bool:
        try:
            self._local_product.remove_async()
        except Exception as error:
            self._set_error(error)
            self.providerStateChanged.emit()
            return False
        self._set_error(None)
        self.providerStateChanged.emit()
        return True

    @Slot(result=bool)
    def cancelLocalAsr(self) -> bool:
        self._local_product.cancel()
        return True

    @Slot(result=bool)
    def refreshLocalAsr(self) -> bool:
        return bool(self._local_product.refresh_async())

    @Slot()
    def shutdown(self) -> None:
        for token in tuple(self._provider_tokens.values()):
            token.cancel()
        self._provider_tokens.clear()
        self._microphone_test_generation += 1
        self._microphone_test_busy = False
        self._local_product.shutdown()

    @Slot(result=bool)
    def load(self) -> bool:
        try:
            loaded_config = self._config_repository.load()
        except Exception as error:  # Repository errors belong in the QML state.
            self._set_error(error)
            return False
        self._replace_loaded_config(loaded_config)
        self._set_error(None)
        self.loaded.emit()
        return True

    @Slot(result=bool)
    def save(self) -> bool:
        persisted_before: AppConfig | None = None
        hotkeys_applied = False
        try:
            persisted_before = self._config_repository.load()
            if (
                self._hotkey_applier is not None
                and persisted_before.hotkeys != self._config.hotkeys
            ):
                self._hotkey_applier(self._config.hotkeys)
                hotkeys_applied = True
            persisted_config = _apply_config_with_autostart_transaction(
                self._config,
                self.repositories,
                self._autostart_registry,
            )
        except Exception as error:  # Validation/storage errors are user-facing.
            if hotkeys_applied and self._hotkey_applier is not None:
                try:
                    assert persisted_before is not None
                    self._hotkey_applier(persisted_before.hotkeys)
                except Exception:
                    pass
            self._set_error(error)
            return False
        self._replace_loaded_config(persisted_config)
        self._set_error(None)
        self.saved.emit()
        return True

    @Slot(str, result=bool)
    def persistMode(self, value: str) -> bool:
        """Persist only the UI mode without applying the editable Settings draft."""

        try:
            normalized = self._choice(value, SUPPORTED_UI_MODES, "mode")
        except ValueError as error:
            self._set_error(error)
            return False
        return self._persist_ui_preference(
            lambda config: replace(
                config,
                ui=replace(config.ui, mode=normalized),
            )
        )

    @Slot(str, result=bool)
    def persistLanguage(self, value: str) -> bool:
        """Persist only the UI language without applying the editable Settings draft."""

        try:
            normalized = self._choice(value, SUPPORTED_LANGUAGES, "language")
        except ValueError as error:
            self._set_error(error)
            return False
        return self._persist_ui_preference(
            lambda config: replace(
                config,
                ui=replace(config.ui, language=normalized),
            )
        )

    @Slot(bool, result=bool)
    def setLocalAsrCloudRefinement(self, value: bool) -> bool:
        return self._update_config(
            replace(self._config, local_asr_cloud_refinement=bool(value))
        )

    def _hotkey_mapping(self, action: HotkeyAction) -> dict[str, Any]:
        settings = self._hotkey_settings_controller.settings
        try:
            definition = settings.definition(action)
        except KeyError:
            # Legacy profiles can omit the post-release voice action. Keep it
            # visible and editable without claiming it is active until Save.
            definition = HotkeySettings.defaults().definition(action)
        mapping = definition.to_mapping()
        return {
            "id": action.value,
            "label": _HOTKEY_ACTION_LABELS[action],
            "display": definition.display,
            "key": mapping["key"],
            "modifiers": list(mapping["modifiers"]),
        }

    def _provider_config(self, provider_id: str):
        return getattr(self._config, provider_id)

    @staticmethod
    def _default_provider_base_url(provider_id: str) -> str:
        return str(PROVIDER_REGISTRY.describe(provider_id).default_base_url or "")

    def _provider_state(self, provider_id: str) -> dict[str, Any]:
        metadata = PROVIDER_REGISTRY.describe(provider_id)
        if provider_id == LOCAL_ASR_PROVIDER_ID:
            state = self._local_state
            return {
                "providerId": provider_id,
                "displayName": metadata.display_name,
                "kind": "local",
                "status": state.status,
                "detail": state.detail,
                "error": state.detail if state.status in {"error", "invalid"} else "",
                "busy": self._local_product.busy,
                "configured": state.status == "installed",
                "hasApiKey": False,
                "baseUrl": "",
                "requirements": _format_local_requirements(state.requirements or {}),
                "progress": state.fraction,
            }
        config = self._provider_config(provider_id)
        activity = self._provider_activity.get(provider_id, {})
        has_key = bool(config.api_key.strip())
        status = str(
            activity.get("status") or ("configured" if has_key else "not_configured")
        )
        return {
            "providerId": provider_id,
            "displayName": metadata.display_name,
            "kind": "cloud",
            "status": status,
            "detail": "",
            "error": str(activity.get("error") or ""),
            "busy": bool(activity.get("busy", False)),
            "configured": has_key and status in {"configured", "active"},
            "hasApiKey": has_key,
            "baseUrl": config.base_url or metadata.default_base_url,
            "models": list(activity.get("models", [])),
            "textModels": list(activity.get("textModels", [])),
        }

    @Slot(str, result=str)
    def providerName(self, provider_id: str) -> str:
        normalized = str(provider_id or "").strip().lower()
        if normalized not in PROVIDER_REGISTRY.provider_ids:
            return normalized
        return PROVIDER_REGISTRY.describe(normalized).display_name

    @Slot(str, int, bool, object)
    def _finish_provider_validation(
        self,
        provider_id: str,
        generation: int,
        success: bool,
        payload: object,
    ) -> None:
        if generation != self._provider_generations.get(provider_id):
            return
        self._provider_tokens.pop(provider_id, None)
        data = payload if isinstance(payload, dict) else {}
        if not success:
            self._provider_activity[provider_id] = {
                "status": "error",
                "error": str(data.get("error") or "Provider validation failed"),
                "models": [],
                "textModels": [],
                "busy": False,
            }
            self._set_error(ValueError(self._provider_activity[provider_id]["error"]))
            self.providerStateChanged.emit()
            return
        try:
            self._persist_provider_credentials(
                provider_id,
                str(data.get("api_key") or ""),
                str(data.get("base_url") or ""),
            )
        except Exception as error:
            self._provider_activity[provider_id] = {
                "status": "error",
                "error": str(error).strip() or type(error).__name__,
                "models": [],
                "textModels": [],
                "busy": False,
            }
            self._set_error(error)
            self.providerStateChanged.emit()
            return
        self._provider_activity[provider_id] = {
            "status": "active",
            "error": "",
            "models": list(data.get("models") or []),
            "textModels": list(data.get("text_models") or []),
            "busy": False,
        }
        if provider_id == self._selected_provider_id:
            self._provider_api_key_draft = ""
            self._provider_base_url_draft = self._config_provider_base_url(provider_id)
        self._set_error(None)
        self.configChanged.emit()
        self.providerStateChanged.emit()

    def _config_provider_base_url(self, provider_id: str) -> str:
        config = self._provider_config(provider_id)
        return config.base_url or self._default_provider_base_url(provider_id)

    def _persist_provider_credentials(
        self,
        provider_id: str,
        api_key: str,
        base_url: str,
        *,
        persisted_config: AppConfig | None = None,
    ) -> AppConfig:
        """Persist only provider credentials, without applying the QML draft."""

        persisted = persisted_config or self._config_repository.load()
        persisted_provider = getattr(persisted, provider_id)
        updated_persisted = replace(
            persisted,
            **{
                provider_id: replace(
                    persisted_provider,
                    api_key=str(api_key or "").strip(),
                    base_url=str(base_url or "").strip(),
                )
            },
        )
        saved = self._config_repository.apply(updated_persisted)

        # Keep unrelated unsaved Settings edits in the local draft while
        # reflecting the credential that was just persisted.
        draft_provider = self._provider_config(provider_id)
        self._config = replace(
            self._config,
            **{
                provider_id: replace(
                    draft_provider,
                    api_key=str(api_key or "").strip(),
                    base_url=str(base_url or "").strip(),
                )
            },
        )
        return saved

    def _publish_local_state(self, state: LocalASRProductState) -> None:
        self._localStatePublished.emit(state)

    @Slot(object)
    def _apply_local_state(self, state: object) -> None:
        if isinstance(state, LocalASRProductState):
            self._local_state = state
            self.providerStateChanged.emit()

    def _microphone_supports_explicit_selection(self) -> bool:
        backend_method = getattr(
            self._microphone_backend,
            "supports_explicit_microphone_selection",
            None,
        )
        if callable(backend_method):
            try:
                return bool(backend_method())
            except Exception:
                return False
        return platform.system() in {"Windows", "Darwin"}

    def _snapshot_microphone_inventory(self) -> MicrophoneInventory:
        backend_snapshot = getattr(
            self._microphone_backend, "microphone_inventory", None
        )
        if callable(backend_snapshot):
            inventory = backend_snapshot()
        elif self._microphone_inventory_source is not None:
            inventory = self._microphone_inventory_source.snapshot()
        else:
            inventory = MicrophoneInventory.unavailable("portaudio_unavailable")
        if not isinstance(inventory, MicrophoneInventory):
            raise TypeError("microphone inventory source returned an invalid snapshot")
        return inventory

    def _microphone_selectable_devices(self) -> tuple[Any, ...]:
        if not self._microphone_supports_explicit_selection():
            return ()
        backend_filter = getattr(
            self._microphone_backend,
            "selectable_microphone_devices",
            None,
        )
        if callable(backend_filter):
            try:
                return tuple(backend_filter(self._microphone_inventory))
            except Exception:
                return ()
        return tuple(self._microphone_inventory.available_devices)

    def _microphone_selection(self) -> MicrophoneSelection:
        return self._microphone_inventory.resolve(self._config.microphone.selected_id)

    def _microphone_selection_for_test(self) -> MicrophoneSelection | None:
        if self._microphone_inventory.error_code == "not_refreshed":
            return None
        requested_id = self._config.microphone.selected_id
        if not self._microphone_supports_explicit_selection():
            requested_id = None
        selection = self._microphone_inventory.resolve(requested_id)
        if selection.state is MicrophoneSelectionState.SELECTED:
            selectable_ids = {
                device.stable_id for device in self._microphone_selectable_devices()
            }
            if selection.selected_id not in selectable_ids:
                return MicrophoneSelection(
                    MicrophoneSelectionState.UNAVAILABLE,
                    requested_id=requested_id,
                    reason="unsafe_backend_name",
                )
        return selection

    def _microphone_status(self) -> tuple[str, str]:
        requested_id = self._config.microphone.selected_id
        if not self._microphone_supports_explicit_selection():
            if requested_id:
                return (
                    "Explicit input selection is unavailable with SoX PulseAudio; "
                    "System default remains active.",
                    "warning",
                )
            return (
                "Explicit input selection is unavailable with SoX PulseAudio; "
                "System default remains active.",
                "info",
            )

        if self._microphone_inventory.error_code in {
            "not_refreshed",
            "portaudio_unavailable",
            "enumeration_failed",
        }:
            return (
                "Input inventory unavailable; System default remains active.",
                "info",
            )

        selection = self._microphone_selection()
        if selection.state is MicrophoneSelectionState.SELECTED:
            selectable_ids = {
                device.stable_id for device in self._microphone_selectable_devices()
            }
            if selection.selected_id not in selectable_ids:
                return (
                    "Saved microphone cannot be routed safely; choose System default.",
                    "error",
                )
        if not selection.can_record:
            return (
                f"No safe input device ({self._microphone_inventory.error_code or 'unavailable'}).",
                "error",
            )
        if selection.is_fallback:
            assert selection.device is not None
            return (
                "Saved microphone unavailable; using current default: "
                + selection.device.name,
                "warning",
            )
        assert selection.device is not None
        return (
            f"{selection.device.name} · {selection.device.host_api or 'default'}",
            "ok",
        )

    @Slot(int, bool, object)
    def _finish_microphone_test(
        self,
        generation: int,
        success: bool,
        payload: object,
    ) -> None:
        if generation != self._microphone_test_generation:
            return
        self._microphone_test_busy = False
        if success:
            try:
                peak = float(payload)
            except (TypeError, ValueError):
                peak = 0.0
            self._microphone_test_status = f"Input level peak: {peak:.2f}"
            self._microphone_test_kind = "ok"
        else:
            message = str(payload or "Input test failed")
            self._microphone_test_status = f"Input test failed: {message}"
            self._microphone_test_kind = "error"
        self.microphoneTestChanged.emit()

    def _selected_route(self) -> WorkflowRoute:
        return self._config.workflow(self._selected_scope)

    @staticmethod
    def _canonical_scope(scope: str | WorkflowScope) -> str:
        if isinstance(scope, WorkflowScope):
            return scope.value
        normalized = str(scope or "").strip().lower()
        if normalized not in WORKFLOW_SCOPES:
            raise ValueError(f"Unknown workflow scope: {normalized or '<empty>'}")
        return normalized

    @staticmethod
    def _choice(value: object, choices: tuple[str, ...], field_name: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in choices:
            choices_text = ", ".join(choices)
            raise ValueError(
                f"Invalid {field_name} '{normalized}'; choose one of: {choices_text}"
            )
        return normalized

    @staticmethod
    def _route_mapping(route: WorkflowRoute, scope: str) -> dict[str, Any]:
        return {
            "scope": scope,
            "providerId": route.provider_id,
            "modelId": route.model_id,
            "prompt": route.prompt,
            "customEndpoint": route.custom_endpoint,
            "enabled": route.enabled,
        }

    def _update_selected_route(
        self, change: Callable[[WorkflowRoute], WorkflowRoute]
    ) -> bool:
        route = replace(change(self._selected_route()), independent=True)
        return self._update_config(
            replace(
                self._config,
                workflows=self._config.workflows.with_route(
                    self._selected_scope, route
                ),
            )
        )

    def _update_config(self, config: AppConfig) -> bool:
        self._set_error(None)
        if config == self._config:
            return True
        hotkeys_changed = config.hotkeys != self._config.hotkeys
        microphone_changed = (
            config.microphone != self._config.microphone
            or config.recording_controls != self._config.recording_controls
        )
        route_changed = config.workflow(self._selected_scope) != self._config.workflow(
            self._selected_scope
        )
        self._config = config
        if hotkeys_changed:
            self._hotkey_settings_controller.replace(config.hotkeys)
            self._hotkey_capture_action = ""
        if not self._dirty:
            self._dirty = True
            self.dirtyChanged.emit()
        self.configChanged.emit()
        if hotkeys_changed:
            self.hotkeyChanged.emit()
        if microphone_changed:
            self.microphoneChanged.emit()
        if route_changed:
            self.routeChanged.emit()
        return True

    def _persist_ui_preference(
        self,
        change: Callable[[AppConfig], AppConfig],
    ) -> bool:
        """Read-modify-write one persisted preference, leaving the draft intact."""

        try:
            persisted_config = self._config_repository.load()
            self._config_repository.apply(change(persisted_config))
        except Exception as error:  # Repository errors belong in the QML state.
            self._set_error(error)
            return False
        self._set_error(None)
        return True

    def _replace_loaded_config(self, config: AppConfig) -> None:
        hotkeys_changed = config.hotkeys != self._config.hotkeys
        capture_changed = bool(self._hotkey_capture_action)
        microphone_changed = (
            config.microphone != self._config.microphone
            or config.recording_controls != self._config.recording_controls
        )
        route_changed = config.workflow(self._selected_scope) != self._config.workflow(
            self._selected_scope
        )
        config_changed = config != self._config
        self._config = config
        self._hotkey_settings_controller.replace(config.hotkeys)
        self._hotkey_capture_action = ""
        self._provider_api_key_draft = ""
        self._provider_base_url_draft = self._config_provider_base_url(
            self._selected_provider_id
        )
        if config_changed:
            self.configChanged.emit()
        if microphone_changed:
            self.microphoneChanged.emit()
        if hotkeys_changed or capture_changed:
            self.hotkeyChanged.emit()
        self.providerStateChanged.emit()
        if route_changed:
            self.routeChanged.emit()
        if self._dirty:
            self._dirty = False
            self.dirtyChanged.emit()

    def _set_error(self, error: BaseException | None) -> None:
        message = "" if error is None else str(error).strip()
        if not message and error is not None:
            message = type(error).__name__
        if message == self._last_error:
            return
        self._last_error = message
        self.errorChanged.emit()


__all__ = ["QmlSettingsController"]
