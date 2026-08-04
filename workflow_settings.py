"""UI-free settings surface for independently editable workflow routes.

The CustomTkinter settings window can use this controller without learning the
on-disk schema or bypassing the provider capability registry.  It keeps a
validated repository snapshot as a draft, exposes safe effective-route
summaries, and delegates apply/reset/test operations to the transactional
repository boundary.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from provider_registry import PROVIDER_REGISTRY
from repositories import (
    AppConfig,
    ConfigRepository,
    WorkflowConfig,
    WorkflowRoute,
    WorkflowScope,
    WorkflowTestResult,
    normalize_workflow_scope,
    validate_workflow_route,
)


_UNSET = object()


class WorkflowSettingsController:
    """Draft and persist independent routes through ``ConfigRepository``."""

    def __init__(self, repository: ConfigRepository, *, registry=PROVIDER_REGISTRY):
        self.repository = repository
        self.registry = registry
        self._config = repository.load()

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def workflows(self) -> WorkflowConfig:
        return self._config.workflows

    def reload(self) -> AppConfig:
        """Discard the draft and reload the repository's effective config."""
        self._config = self.repository.load()
        return self._config

    def route(self, scope: WorkflowScope | str) -> WorkflowRoute:
        return self.workflows.route(scope)

    def set_route(
        self,
        scope: WorkflowScope | str,
        *,
        provider_id: str | object = _UNSET,
        model_id: str | object = _UNSET,
        prompt: str | object = _UNSET,
        custom_endpoint: str | object = _UNSET,
        enabled: bool | object = _UNSET,
    ) -> WorkflowRoute:
        """Update one route in the draft without touching other scopes."""

        current = self.route(scope)
        changes: dict[str, Any] = {}
        for field_name, value in (
            ("provider_id", provider_id),
            ("model_id", model_id),
            ("prompt", prompt),
            ("custom_endpoint", custom_endpoint),
            ("enabled", enabled),
        ):
            if value is not _UNSET:
                changes[field_name] = value
        route = replace(current, **changes)
        self._config = replace(
            self._config,
            workflows=self.workflows.with_route(scope, route),
        )
        return route

    def effective_route(self, scope: WorkflowScope | str) -> dict[str, Any]:
        """Return safe provider/model/local-cloud metadata for the UI."""

        normalized = normalize_workflow_scope(scope)
        route = validate_workflow_route(
            self.route(normalized), normalized, registry=self.registry
        )
        if not route.enabled:
            execution = "disabled"
        elif route.provider_id == "local_asr":
            execution = "local"
        else:
            execution = "cloud"
        return {
            "scope": normalized,
            "provider_id": route.provider_id,
            "model_id": route.model_id,
            "custom_endpoint": route.without_prompt()["custom_endpoint"],
            "enabled": route.enabled,
            "execution": execution,
        }

    def effective_routes(self) -> dict[str, dict[str, Any]]:
        return {
            scope: self.effective_route(scope)
            for scope in (scope.value for scope in WorkflowScope)
        }

    def apply(self) -> AppConfig:
        """Validate and atomically persist the complete draft."""

        self._config = self.repository.apply(self._config)
        return self._config

    def reset(self, scope: WorkflowScope | str) -> AppConfig:
        """Persist one scope's canonical defaults while retaining all others."""

        self._config = self.repository.reset_workflow(scope)
        return self._config

    def test(
        self, scope: WorkflowScope | str | None = None
    ) -> WorkflowTestResult | tuple[WorkflowTestResult, ...]:
        """Run the repository's local, network-free capability check."""

        return self.repository.test_workflow(scope)


__all__ = ["WorkflowSettingsController"]
