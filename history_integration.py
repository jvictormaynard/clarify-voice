"""Application integration for the opt-in local transcription history.

This module keeps the privacy-sensitive store behind a small application
boundary.  Configuration owns the explicit enablement and retention choice;
the history file is only opened when that choice is enabled.  Recording is a
best-effort sink so a history filesystem problem can never fail a live
transcription or clipboard delivery.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from history_store import HistoryRecord, HistoryStore, HistoryStoreError
from repositories import AppConfig, ConfigRepository, HistorySettings


_UNSET = object()


class HistorySettingsController:
    """Coordinate persisted history settings and the local history store."""

    def __init__(
        self,
        repository: ConfigRepository,
        path: str | Path,
        *,
        clock: Callable[[], Any] | None = None,
        store_factory: Callable[..., HistoryStore] = HistoryStore,
    ) -> None:
        self.repository = repository
        self.path = Path(path)
        self._clock = clock
        self._store_factory = store_factory
        self._lock = threading.RLock()
        self._config = repository.load()
        self._store = self._make_store(self._config.history)
        self.last_error: str | None = None

    @property
    def config(self) -> AppConfig:
        with self._lock:
            return self._config

    @property
    def settings(self) -> HistorySettings:
        return self.config.history

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    @property
    def store(self) -> HistoryStore:
        """Expose the store for read-only UI adapters and focused tests."""

        with self._lock:
            return self._store

    def _make_store(self, settings: HistorySettings) -> HistoryStore:
        kwargs: dict[str, Any] = {
            "enabled": settings.enabled,
            "retention_days": settings.retention_days,
        }
        if self._clock is not None:
            kwargs["clock"] = self._clock
        return self._store_factory(self.path, **kwargs)

    def _reload_locked(self) -> None:
        self._config = self.repository.load()
        self._store = self._make_store(self._config.history)

    def startup(self) -> str | None:
        """Reload settings and perform enabled-store recovery/retention."""

        with self._lock:
            self._reload_locked()
            self.last_error = None
            if not self._config.history.enabled:
                return None
            try:
                # Loading is intentionally the startup hook: it recovers an
                # interrupted atomic write and rewrites retained records.
                self._store.list_records()
            except (HistoryStoreError, OSError, ValueError) as error:
                self.last_error = str(error) or error.__class__.__name__
            return self.last_error

    def apply(
        self,
        *,
        enabled: bool | None = None,
        retention_days: int | None | object = _UNSET,
    ) -> HistorySettings:
        """Persist a history setting change and rebuild the store boundary.

        Disabling removes the snapshot before the disabled setting is saved.
        That ordering favors privacy if the second write is interrupted: the
        old transcript contents are already gone and a later startup remains
        disabled by the persisted setting.
        """

        with self._lock:
            current = self._config.history
            payload = current.to_mapping()
            if enabled is not None:
                payload["enabled"] = enabled
            if retention_days is not _UNSET:
                payload["retention_days"] = retention_days
            candidate = HistorySettings.from_mapping(payload)
            if enabled is not None and not isinstance(enabled, bool):
                raise ValueError("enabled must be boolean")
            if retention_days is not _UNSET and (
                retention_days is not None
                and (
                    isinstance(retention_days, bool)
                    or not isinstance(retention_days, int)
                    or retention_days < 0
                )
            ):
                raise ValueError(
                    "retention_days must be a non-negative integer or None"
                )

            self.last_error = None
            if not candidate.enabled:
                # delete_all is deliberately available on a disabled store.
                self._store.delete_all()

            self.repository.save(replace(self._config, history=candidate))
            self._reload_locked()
            if self._config.history.enabled:
                try:
                    self._store.list_records()
                except (HistoryStoreError, OSError, ValueError) as error:
                    self.last_error = str(error) or error.__class__.__name__
            return self._config.history

    def record_transcription(
        self,
        *,
        raw_text: str | None,
        refined_text: str | None,
        provider: str,
        model: str,
        status: str,
        error: str | None,
    ) -> None:
        """Persist one record when enabled; never make it runtime-fatal."""

        with self._lock:
            if not self._config.history.enabled:
                return
            try:
                self._store.add(
                    raw_text=raw_text,
                    refined_text=refined_text,
                    workflow="transcription",
                    provider=provider,
                    model=model,
                    status=status,
                    error=error,
                )
                self.last_error = None
            except (HistoryStoreError, OSError, ValueError) as caught:
                self.last_error = str(caught) or caught.__class__.__name__
            except Exception as caught:
                # Injected repositories and providers must not be able to
                # turn optional history into a transcription failure.
                self.last_error = str(caught) or caught.__class__.__name__

    def records(self) -> list[HistoryRecord]:
        """Return retained records, or an empty list while disabled."""

        with self._lock:
            if not self._config.history.enabled:
                return []
            return self._store.list_records()

    def delete_all(self) -> None:
        """Delete all records, including interrupted-write snapshots."""

        with self._lock:
            self._store.delete_all()
            self.last_error = None

    def export(self, destination: str | Path, *, format: str | None = None) -> Path:
        """Export retained records through the store's safe format boundary."""

        with self._lock:
            return self._store.export(destination, format=format)
