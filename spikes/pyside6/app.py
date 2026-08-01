"""Standalone PySide6 UI spike.

Run with ``python -m spikes.pyside6.app`` after installing the optional spike
dependency. It uses fake data only and deliberately does not import app.py,
provider clients, audio capture, clipboard helpers, or the production hotkey
module.
"""

from __future__ import annotations

import sys
from typing import Callable

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QTimer, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QStyle,
    QVBoxLayout,
    QWidget,
)

try:
    from .model import FakeWorkflow, Surface
except ImportError:  # PyInstaller analyzes this file as a standalone entry point.
    from model import FakeWorkflow, Surface


WINDOW_FLAGS = (
    Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.Tool
)


class DragFrame(QFrame):
    """Small frameless surface that can be dragged without native title bars."""

    def __init__(self, on_drag: Callable[[QPoint], None], parent=None):
        super().__init__(parent)
        self._on_drag = on_drag
        self._drag_origin: QPoint | None = None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.globalPosition().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            current = event.globalPosition().toPoint()
            self._on_drag(current - self._drag_origin)
            self._drag_origin = current
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_origin = None
        super().mouseReleaseEvent(event)


class OverlayPill(QWidget):
    """Always-on-top, transparent status pill used for visual inspection."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(330, 62)
        self.setAccessibleName("Workflow status pill")
        self._text = "Ready · fake workflow"
        self._pulse = QPropertyAnimation(self, b"windowOpacity", self)
        self._pulse.setDuration(220)
        self._pulse.setEasingCurve(QEasingCurve.Type.InOutQuad)

    def set_text(self, text: str) -> None:
        self._text = text
        self.update()
        self._pulse.stop()
        self._pulse.setStartValue(0.72)
        self._pulse.setEndValue(1.0)
        self._pulse.start()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#20242e"))
        painter.setPen(QPen(QColor("#4c566a"), 1))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 28, 28)
        painter.setPen(QColor("#f4f7fb"))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)


class PrototypeWindow(QMainWindow):
    """Fake idle, workflow, result, and settings surfaces in one window."""

    def __init__(self):
        super().__init__()
        self.workflow = FakeWorkflow()
        self.setWindowFlags(WINDOW_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(520, 360)
        self.resize(620, 430)
        self.setWindowTitle("ClarifyVoice · PySide6 spike")
        self.setAccessibleName("ClarifyVoice PySide6 decision spike")
        self._drag_frame = DragFrame(self._move_window, self)
        self.setCentralWidget(self._drag_frame)
        self._layout = QVBoxLayout(self._drag_frame)
        self._layout.setContentsMargins(28, 22, 28, 24)
        self._layout.setSpacing(14)
        self._build_header()
        self._pages = QStackedWidget()
        self._pages.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._layout.addWidget(self._pages)
        self._build_idle_page()
        self._build_result_page()
        self._build_settings_page()
        self._build_footer()
        self._apply_style()
        self._pill = OverlayPill()
        self._refresh()

    def _build_header(self) -> None:
        row = QHBoxLayout()
        title = QLabel("ClarifyVoice")
        title.setStyleSheet("font-size: 24px; font-weight: 700;")
        subtitle = QLabel("PySide6 decision spike · fake workflow")
        subtitle.setStyleSheet("color: #9aa5b5;")
        row.addWidget(title)
        row.addWidget(subtitle)
        row.addStretch()
        close = QPushButton("×")
        close.setAccessibleName("Close prototype")
        close.setFixedWidth(34)
        close.clicked.connect(self.close)
        row.addWidget(close)
        self._layout.addLayout(row)

    def _build_idle_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        intro = QLabel("Test the overlay states and focus-safe surfaces without touching production logic.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self._state_label = QLabel()
        self._state_label.setAccessibleName("Current fake workflow state")
        self._state_label.setStyleSheet("font-size: 18px; font-weight: 600; color: #8be9a8;")
        layout.addWidget(self._state_label)
        actions = QHBoxLayout()
        self._record_button = QPushButton("Record sample")
        self._record_button.setAccessibleName("Record fake sample")
        self._record_button.clicked.connect(self._start_fake_workflow)
        actions.addWidget(self._record_button)
        self._result_button = QPushButton("Show result")
        self._result_button.setAccessibleName("Show fake result")
        self._result_button.clicked.connect(lambda: self._set_workflow("show_result"))
        actions.addWidget(self._result_button)
        settings_button = QPushButton("Settings")
        settings_button.setAccessibleName("Open fake settings")
        settings_button.clicked.connect(self._open_settings)
        actions.addWidget(settings_button)
        layout.addLayout(actions)
        layout.addStretch()
        self._pages.addWidget(page)

    def _build_result_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("Result panel")
        title.setStyleSheet("font-size: 20px; font-weight: 600;")
        layout.addWidget(title)
        self._result_label = QLabel()
        self._result_label.setWordWrap(True)
        self._result_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._result_label.setAccessibleName("Fake result text")
        self._result_label.setFrameShape(QFrame.Shape.StyledPanel)
        layout.addWidget(self._result_label)
        back = QPushButton("Back to idle")
        back.clicked.connect(lambda: self._set_workflow("reset"))
        layout.addWidget(back)
        self._pages.addWidget(page)

    def _build_settings_page(self) -> None:
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow(QLabel("Settings (fake)"))
        language = QLabel(self.workflow.language)
        form.addRow("Language", language)
        always_on_top = QCheckBox("Keep the prototype above other windows")
        always_on_top.setChecked(True)
        always_on_top.setAccessibleName("Always on top setting")
        form.addRow("Overlay", always_on_top)
        hotkey = QLabel(self.workflow.hotkey_hint)
        hotkey.setWordWrap(True)
        form.addRow("Hotkey", hotkey)
        back = QPushButton("Close settings")
        back.clicked.connect(lambda: self._set_workflow("close_settings"))
        form.addRow(back)
        self._pages.addWidget(page)

    def _build_footer(self) -> None:
        self._footer = QLabel("Qt 6 · automatic DPI scaling · no global hotkey registration")
        self._footer.setStyleSheet("color: #9aa5b5; font-size: 11px;")
        self._layout.addWidget(self._footer)

    def _apply_style(self) -> None:
        self._drag_frame.setStyleSheet(
            "QFrame { background: #151922; border: 1px solid #303746; border-radius: 18px; }"
            "QPushButton { background: #293246; border: 1px solid #4c566a; border-radius: 8px; padding: 8px 12px; }"
            "QPushButton:hover { background: #39465e; }"
            "QLabel { color: #f4f7fb; }"
        )

    def _move_window(self, delta: QPoint) -> None:
        self.move(self.pos() + delta)
        self._position_pill()

    def _position_pill(self) -> None:
        self._pill.move(
            self.x() + (self.width() - self._pill.width()) // 2,
            self.y() - 76,
        )

    def reveal(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self._position_pill()
        self._pill.show()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._position_pill()
        self._pill.show()

    def _start_fake_workflow(self) -> None:
        if self.workflow.surface != Surface.IDLE:
            return
        self._set_workflow("record")
        QTimer.singleShot(500, lambda: self._set_workflow("process"))
        QTimer.singleShot(1_200, lambda: self._set_workflow("complete"))

    def _set_workflow(self, event: str) -> None:
        try:
            self.workflow = self.workflow.transition(event)
        except ValueError:
            return
        self._refresh()

    def _open_settings(self) -> None:
        self.workflow = self.workflow.open_settings()
        self._refresh()

    def _refresh(self) -> None:
        surface = self.workflow.surface
        pages = {Surface.IDLE: 0, Surface.RECORDING: 0, Surface.PROCESSING: 0, Surface.SUCCESS: 0,
                 Surface.RESULT: 1, Surface.SETTINGS: 2}
        self._pages.setCurrentIndex(pages[surface])
        label = {
            Surface.IDLE: "Idle surface",
            Surface.RECORDING: "Recording · fake audio",
            Surface.PROCESSING: "Processing · fake provider",
            Surface.SUCCESS: "Success · ready to review",
            Surface.RESULT: "Result panel",
            Surface.SETTINGS: "Settings page",
        }[surface]
        self._state_label.setText(label)
        self._record_button.setEnabled(surface == Surface.IDLE)
        self._result_button.setEnabled(surface == Surface.SUCCESS)
        self._result_label.setText(self.workflow.result)
        self._pill.set_text(label)

    def closeEvent(self, event) -> None:
        self._pill.hide()
        self.hide()
        event.ignore()


def _build_tray(window: PrototypeWindow) -> QSystemTrayIcon:
    tray = QSystemTrayIcon(window)
    tray.setIcon(QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
    tray.setToolTip("ClarifyVoice PySide6 spike")
    menu = tray.contextMenu()
    if menu is None:
        from PySide6.QtWidgets import QMenu

        menu = QMenu(window)
        tray.setContextMenu(menu)
    show = QAction("Show prototype", window)
    show.triggered.connect(window.reveal)
    menu.addAction(show)
    quit_action = QAction("Quit", window)
    quit_action.triggered.connect(QApplication.quit)
    menu.addAction(quit_action)
    tray.show()
    return tray


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("ClarifyVoice PySide6 spike")
    window = PrototypeWindow()
    _tray = _build_tray(window)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
