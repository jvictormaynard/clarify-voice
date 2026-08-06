"""Real Qt Quick/QML entrypoint for ClarifyVoice dictation."""

from __future__ import annotations

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


def _start_shell_if_available(shell, hotkeys) -> bool | None:
    """Start the native shell when the desktop provides a system tray.

    ``None`` means the application is running without a tray.  A failed shell
    start is isolated to shell-owned resources so the QML window can remain
    usable; both the shell and backend are asked to clean up their resources.
    """

    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None
    try:
        return bool(shell.start())
    except Exception as error:
        try:
            shell.stop()
        finally:
            if hotkeys is not None:
                hotkeys.stop()
        print(
            f"ClarifyVoice QML shell unavailable: {error}",
            file=sys.stderr,
        )
        return False


def _register_qml_context(engine, workflow, settings) -> None:
    """Expose the two real Qt-facing controllers before QML is loaded."""

    context = engine.rootContext()
    context.setContextProperty("workflow", workflow)
    context.setContextProperty("settings", settings)


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

    bridge = QmlWorkflowBridge(
        workflow_service,
        dispatch_runner=scheduler.run_dispatch,
        copy_runner=runtime.copy_result,
        parent=app,
    )
    settings = QmlSettingsController(repositories, parent=app)
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
    _connect_shutdown(app, shell, runtime)

    shell_started = _start_shell_if_available(shell, hotkeys)
    if shell_started is False:
        runtime.shutdown()
        return 0
    app.setQuitOnLastWindowClosed(shell_started is None)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
