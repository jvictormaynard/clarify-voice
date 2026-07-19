"""Small explicit state controllers for ClarifyVoice desktop workflows."""

from __future__ import annotations


class WorkflowController:
    """Guarantee that rewrite and translation cannot overlap."""

    def __init__(self):
        self._active: str | None = None

    @property
    def active(self) -> str | None:
        return self._active

    def is_active(self, workflow: str) -> bool:
        return self._active == workflow

    def start(self, workflow: str) -> bool:
        if self._active not in (None, workflow):
            return False
        self._active = workflow
        return True

    def finish(self, workflow: str) -> None:
        if self._active == workflow:
            self._active = None

    def any_active(self) -> bool:
        return self._active is not None
