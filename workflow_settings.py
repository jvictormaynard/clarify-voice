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
    test_workflow_configuration,
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
        normalized = normalize_workflow_scope(scope)
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
        if any(
                value is not _UNSET
                and value != getattr(current, field_name)
                for field_name, value in (
                    ("provider_id", provider_id),
                    ("model_id", model_id),
                    ("prompt", prompt),
                    ("custom_endpoint", custom_endpoint),
                    ("enabled", enabled),
                )
        ):
            # The marker records authorship at the moment a scoped form is
            # actually edited.  Merely opening/applying an untouched legacy
            # route must not opt it out of flat compatibility migration.
            changes["independent"] = True
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

    def sync_persisted_route(
        self, scope: WorkflowScope | str, route: WorkflowRoute
    ) -> AppConfig:
        """Reflect one external route save without discarding other drafts."""

        normalized = normalize_workflow_scope(scope)
        self._config = replace(
            self._config,
            workflows=self.workflows.with_route(normalized, route),
            local_asr_cloud_refinement=(
                route.enabled
                if normalized == WorkflowScope.LOCAL_ASR_REFINEMENT.value
                else self._config.local_asr_cloud_refinement
            ),
        )
        return self._config

    def sync_local_asr_refinement(self, enabled: bool) -> AppConfig:
        """Reflect an immediate legacy preference save in the current draft.

        The local-ASR provider page persists this safety-sensitive opt-in
        immediately, before the user presses the settings window's global
        Apply button.  Update only the corresponding typed route and legacy
        compatibility flag so unrelated workflow drafts remain untouched.
        """

        normalized = WorkflowScope.LOCAL_ASR_REFINEMENT.value
        route = replace(self.route(normalized), enabled=bool(enabled))
        return self.sync_persisted_route(normalized, route)

    def reset(self, scope: WorkflowScope | str) -> AppConfig:
        """Persist one scope's canonical defaults while retaining all others."""

        persisted = self.repository.reset_workflow(scope)
        normalized = normalize_workflow_scope(scope)
        # ``reset_workflow`` intentionally operates on the persisted snapshot.
        # Keep any edits in the other draft scopes while replacing only the
        # route that was actually reset (and its legacy local-ASR flag).
        self._config = replace(
            self._config,
            workflows=self.workflows.with_route(
                normalized, persisted.workflow(normalized)
            ),
            local_asr_cloud_refinement=(
                persisted.local_asr_cloud_refinement
                if normalized == WorkflowScope.LOCAL_ASR_REFINEMENT.value
                else self._config.local_asr_cloud_refinement
            ),
        )
        return self._config

    def test(
        self, scope: WorkflowScope | str | None = None
    ) -> WorkflowTestResult | tuple[WorkflowTestResult, ...]:
        """Run a local, network-free capability check against the draft."""

        return test_workflow_configuration(
            self._config.workflows, scope, registry=self.registry
        )


__all__ = ["WorkflowSettingsController"]
