"""Qt-free fake workflow model used by the PySide6 decision spike."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class Surface(str, Enum):
    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"
    SUCCESS = "success"
    RESULT = "result"
    SETTINGS = "settings"


@dataclass(frozen=True)
class FakeWorkflow:
    """Deterministic UI-only workflow; no provider or recording calls."""

    surface: Surface = Surface.IDLE
    transcript: str = "A short fake transcript for the UI spike."
    result: str = "A polished result panel with no provider dependency."
    language: str = "English"
    hotkey_hint: str = "Alt+Space remains owned by the production shell."

    def transition(self, event: str) -> "FakeWorkflow":
        transitions = {
            (Surface.IDLE, "record"): Surface.RECORDING,
            (Surface.RECORDING, "process"): Surface.PROCESSING,
            (Surface.PROCESSING, "complete"): Surface.SUCCESS,
            (Surface.SUCCESS, "show_result"): Surface.RESULT,
            (Surface.RESULT, "reset"): Surface.IDLE,
            (Surface.SETTINGS, "close_settings"): Surface.IDLE,
        }
        next_surface = transitions.get((self.surface, event))
        if next_surface is None:
            raise ValueError(f"unsupported transition: {self.surface.value} + {event}")
        return replace(self, surface=next_surface)

    def open_settings(self) -> "FakeWorkflow":
        return replace(self, surface=Surface.SETTINGS)
