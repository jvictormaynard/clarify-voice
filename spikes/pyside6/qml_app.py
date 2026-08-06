"""Real Qt Quick/QML entrypoint for ClarifyVoice dictation."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

try:
    from .qml_bridge import QmlWorkflowBridge
    from .qml_runtime import (
        QtRuntimeError,
        QtWorkflowScheduler,
        create_real_workflow_runtime,
    )
except ImportError:  # PyInstaller analyzes this file as a standalone entry point.
    from qml_bridge import QmlWorkflowBridge
    from qml_runtime import (
        QtRuntimeError,
        QtWorkflowScheduler,
        create_real_workflow_runtime,
    )


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

    app = QGuiApplication(sys.argv[:1])
    app.setApplicationName("ClarifyVoice")
    app.setOrganizationName("ClarifyVoice")

    scheduler = QtWorkflowScheduler(app)
    try:
        runtime = create_real_workflow_runtime(scheduler)
    except QtRuntimeError as error:
        print(f"ClarifyVoice QML startup failed: {error}", file=sys.stderr)
        return 2

    workflow_service = runtime.workflow_service
    app.aboutToQuit.connect(runtime.shutdown)

    bridge = QmlWorkflowBridge(
        workflow_service,
        dispatch_runner=scheduler.run_dispatch,
        copy_runner=runtime.copy_result,
        parent=app,
    )
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("workflow", bridge)

    qml_root = Path(__file__).with_name("qml")
    engine.addImportPath(str(qml_root))
    engine.load(QUrl.fromLocalFile(str(qml_root / "Main.qml")))
    if not engine.rootObjects():
        runtime.shutdown()
        return 1
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
