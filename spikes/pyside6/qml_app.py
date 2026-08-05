"""Standalone Qt Quick/QML visual pilot for ClarifyVoice.

This entry point intentionally uses fake data only. It does not import the
production application, providers, recording, clipboard, hotkey, or settings
implementations. The bridge is the seam that a later UI migration can replace
with the existing headless workflow protocols.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Property, QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

try:
    from .model import FakeWorkflow, Surface
except ImportError:  # PyInstaller analyzes this file as a standalone entry point.
    from model import FakeWorkflow, Surface


class QmlWorkflowBridge(QObject):
    """Small observable fake workflow exposed to QML."""

    surfaceChanged = Signal()
    statusChanged = Signal()
    resultChanged = Signal()
    busyChanged = Signal()
    canShowResultChanged = Signal()

    _STATUS = {
        Surface.IDLE: "Ready to capture your voice",
        Surface.RECORDING: "Listening to your microphone",
        Surface.PROCESSING: "Polishing your words",
        Surface.SUCCESS: "Your result is ready",
        Surface.RESULT: "Review your result",
        Surface.SETTINGS: "Prototype settings",
    }

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._workflow = FakeWorkflow()

    @Property(str, notify=surfaceChanged)
    def surface(self) -> str:
        return self._workflow.surface.value

    @Property(str, notify=statusChanged)
    def status(self) -> str:
        return self._STATUS[self._workflow.surface]

    @Property(str, notify=resultChanged)
    def result(self) -> str:
        return self._workflow.result

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._workflow.surface in {
            Surface.RECORDING,
            Surface.PROCESSING,
        }

    @Property(bool, notify=canShowResultChanged)
    def canShowResult(self) -> bool:
        return self._workflow.surface == Surface.SUCCESS

    def _notify_all(self) -> None:
        self.surfaceChanged.emit()
        self.statusChanged.emit()
        self.resultChanged.emit()
        self.busyChanged.emit()
        self.canShowResultChanged.emit()

    def _transition(self, event: str) -> None:
        try:
            self._workflow = self._workflow.transition(event)
        except ValueError:
            return
        self._notify_all()

    @Slot()
    def startRecording(self) -> None:
        if self._workflow.surface != Surface.IDLE:
            return
        self._transition("record")
        QTimer.singleShot(650, lambda: self._transition("process"))
        QTimer.singleShot(1_450, lambda: self._transition("complete"))

    @Slot()
    def showResult(self) -> None:
        if self._workflow.surface == Surface.SUCCESS:
            self._transition("show_result")

    @Slot()
    def reset(self) -> None:
        if self._workflow.surface in {Surface.RESULT, Surface.SETTINGS}:
            self._workflow = FakeWorkflow()
            self._notify_all()

    @Slot()
    def openSettings(self) -> None:
        if not self.busy:
            self._workflow = self._workflow.open_settings()
            self._notify_all()

    @Slot()
    def closeSettings(self) -> None:
        if self._workflow.surface == Surface.SETTINGS:
            self._transition("close_settings")


def main() -> int:
    app = QGuiApplication(sys.argv)
    app.setApplicationName("ClarifyVoice QML pilot")
    app.setOrganizationName("ClarifyVoice")

    engine = QQmlApplicationEngine()
    bridge = QmlWorkflowBridge(app)
    engine.rootContext().setContextProperty("workflow", bridge)

    qml_root = Path(__file__).with_name("qml")
    engine.addImportPath(str(qml_root))
    engine.load(QUrl.fromLocalFile(str(qml_root / "Main.qml")))
    if not engine.rootObjects():
        return 1
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
