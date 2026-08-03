"""Application-facing lifecycle for the optional local-ASR assets.

The installer and sidecar implementation deliberately stay independent from
the Tk view.  This small controller supplies the missing product boundary:
explicit user authorization, observable progress, cancellation, actionable
failure states, and bounded shutdown ownership.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from local_asr import (
    LocalASRCancelledError,
    LocalASRError,
    LocalASRInstaller,
)


@dataclass(frozen=True)
class LocalASRProductState:
    """Immutable state safe to copy across the worker/UI boundary."""

    status: str
    stage: str = ""
    current: int = 0
    total: int = 0
    detail: str = ""
    requirements: dict | None = None

    @property
    def fraction(self) -> float:
        if self.total <= 0:
            return 0.0
        return max(0.0, min(1.0, self.current / self.total))


StateListener = Callable[[LocalASRProductState], None]


class LocalASRProductController:
    """Own explicit local-ASR installation/removal operations.

    ``install`` is never called during construction or normal startup.  The
    caller must invoke :meth:`install_async` after showing the requirements
    and obtaining an explicit user decision.  Worker callbacks are delivered
    on the worker thread; a Tk caller should schedule them through ``after``.
    """

    def __init__(
        self,
        installer: LocalASRInstaller | None = None,
        *,
        backend=None,
        listener: StateListener | None = None,
    ) -> None:
        self.installer = installer or LocalASRInstaller()
        self.backend = backend
        self._listeners: list[StateListener] = []
        if listener is not None:
            self._listeners.append(listener)
        self._lock = threading.RLock()
        self._cancel_event: threading.Event | None = None
        self._worker: threading.Thread | None = None
        try:
            requirements = dict(self.installer.requirements())
        except Exception:
            requirements = {}
        self._state = LocalASRProductState(
            status="checking",
            stage="checking",
            detail="Checking the installed local-ASR assets…",
            requirements=requirements,
        )

    @staticmethod
    def _state_from_status(status: dict) -> LocalASRProductState:
        state = str(status.get("state", "invalid"))
        detail = str(status.get("detail", ""))
        return LocalASRProductState(
            status=state,
            detail=detail,
            requirements=dict(status.get("requirements") or {}),
        )

    @property
    def state(self) -> LocalASRProductState:
        with self._lock:
            return self._state

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._worker is not None and self._worker.is_alive()

    def subscribe(self, listener: StateListener) -> None:
        with self._lock:
            self._listeners.append(listener)
            state = self._state
        listener(state)

    def _publish(self, state: LocalASRProductState) -> None:
        with self._lock:
            self._state = state
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(state)
            except Exception:
                # A closed Tk window must not leave an installation worker
                # holding the asset-root lock or suppress cleanup.
                continue

    def refresh(self) -> LocalASRProductState:
        """Read-only status refresh; this performs no network operation."""
        state = self._state_from_status(self.installer.status())
        self._publish(state)
        return state

    def refresh_async(self) -> bool:
        """Verify installed assets away from the Tk/startup thread."""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return False

        def run() -> None:
            try:
                state = self._state_from_status(self.installer.status())
            except (LocalASRError, OSError) as error:
                state = LocalASRProductState(
                    status="error",
                    detail=self._actionable_error(error),
                    requirements=self.installer.requirements(),
                )
            finally:
                with self._lock:
                    if self._worker is threading.current_thread():
                        self._worker = None
            self._publish(state)

        worker = threading.Thread(
            target=run, name="ClarifyVoiceLocalASRStatus", daemon=True)
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return False
            self._worker = worker
        worker.start()
        return True

    def _begin(self, operation: str) -> threading.Event:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                raise LocalASRError("A local-ASR asset operation is already running")
            cancel_event = threading.Event()
            self._cancel_event = cancel_event
            state = self._state
            requirements = state.requirements or self.installer.requirements()
            self._publish(LocalASRProductState(
                status=operation,
                stage="preparing",
                detail=("Preparing the verified local-ASR asset operation"),
                requirements=dict(requirements),
            ))
            return cancel_event

    def install_async(self) -> None:
        """Start an explicitly authorized install in a background worker."""
        cancel_event = self._begin("installing")

        def progress(stage: str, current: int, total: int) -> None:
            with self._lock:
                requirements = self._state.requirements
            self._publish(LocalASRProductState(
                status="installing",
                stage=str(stage),
                current=max(0, int(current)),
                total=max(0, int(total)),
                detail=self._progress_detail(stage, current, total),
                requirements=requirements,
            ))

        def run() -> None:
            try:
                status = self.installer.install(progress, cancel_event)
            except LocalASRCancelledError as error:
                self._publish(LocalASRProductState(
                    status="cancelled",
                    detail=str(error),
                    requirements=self.installer.requirements(),
                ))
            except (LocalASRError, OSError) as error:
                self._publish(LocalASRProductState(
                    status="error",
                    detail=self._actionable_error(error),
                    requirements=self.installer.requirements(),
                ))
            else:
                self._publish(self._state_from_status(status))
            finally:
                with self._lock:
                    self._cancel_event = None
                    self._worker = None

        worker = threading.Thread(
            target=run, name="ClarifyVoiceLocalASRInstall", daemon=True)
        with self._lock:
            self._worker = worker
        worker.start()

    def remove_async(self) -> None:
        """Remove only installer-owned assets in a background worker."""
        cancel_event = self._begin("removing")

        def run() -> None:
            try:
                if cancel_event.is_set():
                    raise LocalASRCancelledError("Local ASR removal was cancelled")
                backend = self.backend
                stop = getattr(backend, "stop", None)
                if callable(stop):
                    stop()
                if cancel_event.is_set():
                    raise LocalASRCancelledError("Local ASR removal was cancelled")
                self.installer.remove(cancel_event=cancel_event)
            except LocalASRCancelledError as error:
                self._publish(LocalASRProductState(
                    status="cancelled",
                    detail=str(error),
                    requirements=self.installer.requirements(),
                ))
            except (LocalASRError, OSError) as error:
                self._publish(LocalASRProductState(
                    status="error",
                    detail=self._actionable_error(error),
                    requirements=self.installer.requirements(),
                ))
            else:
                self._publish(LocalASRProductState(
                    status="not_installed",
                    detail="Local-ASR assets were removed.",
                    requirements=self.installer.requirements(),
                ))
            finally:
                with self._lock:
                    self._cancel_event = None
                    self._worker = None

        worker = threading.Thread(
            target=run, name="ClarifyVoiceLocalASRRemove", daemon=True)
        with self._lock:
            self._worker = worker
        worker.start()

    def cancel(self) -> None:
        with self._lock:
            event = self._cancel_event
        if event is not None:
            event.set()

    def shutdown(self, timeout: float = 2.0) -> None:
        """Cancel installation and wait briefly before app teardown."""
        self.cancel()
        with self._lock:
            worker = self._worker
        if worker is not None:
            worker.join(timeout=max(0.0, float(timeout)))

    @staticmethod
    def _progress_detail(stage: str, current: int, total: int) -> str:
        stage = str(stage).replace("_", " ").replace(":", " / ")
        if total > 0:
            return f"{stage}: {int(current):,} / {int(total):,} bytes"
        return stage

    @staticmethod
    def _actionable_error(error: BaseException) -> str:
        message = str(error).strip() or type(error).__name__
        return (
            f"{message} Check free disk space, network access, and the published "
            "manifest, then retry."
        )


def format_requirement_bytes(value: int) -> str:
    """Human-readable binary size for the settings view."""
    number = max(0, int(value))
    units = ("B", "KiB", "MiB", "GiB")
    amount = float(number)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"


def format_requirements(requirements: dict | None) -> str:
    values = requirements or {}
    return (
        f"{values.get('platform', 'Windows x64')} · "
        f"{format_requirement_bytes(values.get('memory_bytes', 0))} RAM · "
        f"{format_requirement_bytes(values.get('disk_bytes', 0))} free disk · "
        f"{format_requirement_bytes(values.get('download_bytes', 0))} download"
    )
