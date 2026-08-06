"""Real Qt Quick/QML entrypoint for ClarifyVoice dictation."""

from __future__ import annotations

from enum import Enum
import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

try:
    from .qml_bridge import QmlWorkflowBridge
    from .qml_settings import QmlSettingsController
    from .qml_runtime import (
        QtRuntimeError,
        QtWorkflowScheduler,
        create_real_workflow_runtime,
    )
    from .qt_shell import QtShell, WindowsGlobalHotkeyBackend
except ImportError:  # PyInstaller analyzes this file as a standalone entry point.
    from qml_bridge import QmlWorkflowBridge
    from qml_settings import QmlSettingsController
    from qml_runtime import (
        QtRuntimeError,
        QtWorkflowScheduler,
        create_real_workflow_runtime,
    )
    from qt_shell import QtShell, WindowsGlobalHotkeyBackend


class ShellStartResult(Enum):
    """Describe the shell outcome without conflating setup errors and peers."""

    STARTED = "started"
    STARTED_WITHOUT_TRAY = "started_without_tray"
    SECONDARY_INSTANCE = "secondary_instance"
    SETUP_FAILED = "setup_failed"


def _start_shell_if_available(shell, hotkeys) -> ShellStartResult:
    """Start the native shell and retain a usable QML window on setup errors.

    The shell always owns the single-instance guard.  Only tray construction is
    conditional, while a failed optional shell setup is isolated to shell-owned
    resources so the QML window can remain usable.  A secondary process is the
    only outcome that should prevent the workflow runtime from continuing.
    """

    tray_available = bool(QSystemTrayIcon.isSystemTrayAvailable())
    try:
        if not shell.start(tray_available=tray_available):
            return ShellStartResult.SECONDARY_INSTANCE
    except Exception as error:
        for resource in (shell, hotkeys):
            if resource is None:
                continue
            try:
                resource.stop()
            except Exception as cleanup_error:
                print(
                    f"ClarifyVoice QML shell cleanup failed: {cleanup_error}",
                    file=sys.stderr,
                )
        print(
            f"ClarifyVoice QML shell unavailable: {error}",
            file=sys.stderr,
        )
        return ShellStartResult.SETUP_FAILED

    return (
        ShellStartResult.STARTED
        if tray_available
        else ShellStartResult.STARTED_WITHOUT_TRAY
    )


def _register_qml_context(engine, workflow, settings) -> None:
    """Expose the two real Qt-facing controllers before QML is loaded."""

    context = engine.rootContext()
    context.setContextProperty("workflow", workflow)
    context.setContextProperty("settings", settings)


def _connect_preference_sync(bridge, settings) -> None:
    """Keep the persisted draft and compact workflow controls in one state.

    Both controller APIs ignore unchanged values, so these reciprocal signal
    connections converge without a re-entrancy flag or feedback loop.
    """

    settings.configChanged.connect(lambda: bridge.setMode(settings.mode))
    settings.configChanged.connect(lambda: bridge.setLanguage(settings.language))
    bridge.modeChanged.connect(lambda: settings.setMode(bridge.mode))
    bridge.languageChanged.connect(lambda: settings.setLanguage(bridge.language))


def _show_translation_picker_if_needed(bridge, shell) -> None:
    """Reveal a picker that was opened by a hotkey while the window was hidden."""

    if bridge.surface == "translation_picker":
        shell.show_window()


def _connect_shutdown(app, shell, runtime) -> None:
    """Stop shell-owned native resources before the workflow runtime."""

    app.aboutToQuit.connect(shell.stop)
    app.aboutToQuit.connect(runtime.shutdown)


def main(argv: list[str] | None = None) -> int:
    """Start the real QML frontend; missing runtime dependencies are fatal."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        print(
            "ClarifyVoice QML does not accept compatibility/fake runtime flags; "
            "start it without arguments.",
            file=sys.stderr,
        )
        return 2

    app = QApplication(sys.argv[:1])
    app.setApplicationName("ClarifyVoice")
    app.setOrganizationName("ClarifyVoice")

    scheduler = QtWorkflowScheduler(app)
    try:
        runtime = create_real_workflow_runtime(scheduler)
    except QtRuntimeError as error:
        print(f"ClarifyVoice QML startup failed: {error}", file=sys.stderr)
        return 2

    workflow_service = runtime.workflow_service
    repositories = runtime.repositories
    if repositories is None:
        runtime.shutdown()
        print(
            "ClarifyVoice QML startup failed: runtime repositories are missing",
            file=sys.stderr,
        )
        return 2

    loaded_config = repositories.config.load()
    bridge = QmlWorkflowBridge(
        workflow_service,
        app_config=loaded_config,
        dispatch_runner=scheduler.run_dispatch,
        copy_runner=runtime.copy_result,
        parent=app,
    )
    settings = QmlSettingsController(repositories, parent=app)
    _connect_preference_sync(bridge, settings)
    engine = QQmlApplicationEngine()
    _register_qml_context(engine, bridge, settings)

    qml_root = Path(__file__).with_name("qml")
    engine.addImportPath(str(qml_root))
    engine.load(QUrl.fromLocalFile(str(qml_root / "Main.qml")))
    if not engine.rootObjects():
        runtime.shutdown()
        return 1

    window = engine.rootObjects()[0]
    hotkeys = None
    if sys.platform == "win32":
        hotkeys = WindowsGlobalHotkeyBackend(
            app,
            settings=repositories.config.load().hotkeys,
            parent=app,
        )
    shell = QtShell(window, hotkeys=hotkeys, application=app, parent=app)
    shell.hotkeyTriggered.connect(bridge.handleHotkey)
    bridge.surfaceChanged.connect(
        lambda: _show_translation_picker_if_needed(bridge, shell)
    )
    _connect_shutdown(app, shell, runtime)

    shell_result = _start_shell_if_available(shell, hotkeys)
    if shell_result is ShellStartResult.SECONDARY_INSTANCE:
        runtime.shutdown()
        return 0
    app.setQuitOnLastWindowClosed(
        shell_result
        in (ShellStartResult.STARTED_WITHOUT_TRAY, ShellStartResult.SETUP_FAILED)
    )
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
