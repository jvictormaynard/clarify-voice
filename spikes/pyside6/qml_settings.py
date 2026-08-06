"""Typed Qt Quick settings controller for the ClarifyVoice frontend.

The controller keeps an :class:`repositories.AppConfig` draft and exposes
only typed scalar properties and workflow-route mappings to QML.  Persistence
is delegated to ``ApplicationRepositories.config.apply`` so route validation
and atomic storage remain owned by the existing repository boundary.  Windows
autostart is handled by a small, local Run-key boundary.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import Property, QObject, Signal, Slot

from provider_registry import PROVIDER_REGISTRY
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


AUTOSTART_REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE_NAME = "ClarifyVoice"


def _is_windows() -> bool:
    return sys.platform == "win32"


def _registry_module(registry: Any | None = None) -> Any:
    if registry is not None:
        return registry
    import winreg

    return winreg


def _autostart_command(executable: str | None = None) -> str:
    executable = str(executable or sys.executable)
    arguments = [executable]
    if not getattr(sys, "frozen", False):
        arguments.append(str(Path(__file__).with_name("qml_app.py").resolve()))
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

    def __init__(
        self,
        repositories: ApplicationRepositories,
        parent: QObject | None = None,
        *,
        registry: Any | None = None,
    ) -> None:
        super().__init__(parent)
        self.repositories = repositories
        self._config_repository: ConfigRepository = repositories.config
        self._autostart_registry = registry
        self._selected_scope = WorkflowScope.TRANSCRIPTION.value
        self._config = self._config_repository.load()
        self._dirty = False
        self._last_error = ""

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
        try:
            persisted_config = _apply_config_with_autostart_transaction(
                self._config,
                self.repositories,
                self._autostart_registry,
            )
        except Exception as error:  # Validation/storage errors are user-facing.
            self._set_error(error)
            return False
        self._replace_loaded_config(persisted_config)
        self._set_error(None)
        self.saved.emit()
        return True

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
        route_changed = config.workflow(self._selected_scope) != self._config.workflow(
            self._selected_scope
        )
        self._config = config
        if not self._dirty:
            self._dirty = True
            self.dirtyChanged.emit()
        self.configChanged.emit()
        if route_changed:
            self.routeChanged.emit()
        return True

    def _replace_loaded_config(self, config: AppConfig) -> None:
        route_changed = config.workflow(self._selected_scope) != self._config.workflow(
            self._selected_scope
        )
        config_changed = config != self._config
        self._config = config
        if config_changed:
            self.configChanged.emit()
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
