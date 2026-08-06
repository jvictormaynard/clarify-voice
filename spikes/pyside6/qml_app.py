"""Real Qt Quick/QML entrypoint for ClarifyVoice dictation."""

from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path

# Source autostart executes this file directly from ``spikes/pyside6`` while
# the root-level workflow modules remain package imports.  Put the repository
# root on ``sys.path`` before importing either the Qt or application modules.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from PySide6.QtCore import QUrl  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402


def _branding_icon_path() -> Path:
    """Resolve the bundled branding icon for source and frozen launches."""

    roots: list[Path] = []
    bundled_root = getattr(sys, "_MEIPASS", None)
    if bundled_root:
        roots.append(Path(bundled_root))
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    roots.append(_REPOSITORY_ROOT)

    seen: set[Path] = set()
    relative_path = Path("assets") / "branding" / "clarify.ico"
    for root in roots:
        resolved_root = root.resolve()
        if resolved_root in seen:
            continue
        seen.add(resolved_root)
        candidate = resolved_root / relative_path
        if candidate.is_file():
            return candidate

    raise FileNotFoundError(
        f"ClarifyVoice branding icon was not found under: {', '.join(map(str, roots))}"
    )


def _load_branding_icon() -> QIcon:
    """Load a non-null QIcon for the native tray shell."""

    icon = QIcon(str(_branding_icon_path()))
    if icon.isNull():
        raise RuntimeError("ClarifyVoice branding icon could not be loaded")
    return icon


try:
    from .qml_bridge import QmlWorkflowBridge  # noqa: E402
    from .qml_settings import QmlSettingsController  # noqa: E402
    from .qml_runtime import (  # noqa: E402
        QtRuntimeError,
        QtWorkflowScheduler,
        create_real_workflow_runtime,
    )
    from .qt_shell import QtShell, WindowsGlobalHotkeyBackend  # noqa: E402
except ImportError:  # PyInstaller analyzes this file as a standalone entry point.
    from qml_bridge import QmlWorkflowBridge  # noqa: E402
    from qml_settings import QmlSettingsController  # noqa: E402
    from qml_runtime import (  # noqa: E402
        QtRuntimeError,
        QtWorkflowScheduler,
        create_real_workflow_runtime,
    )
    from qt_shell import QtShell, WindowsGlobalHotkeyBackend  # noqa: E402


class ShellStartResult(Enum):
    """Describe the shell outcome without conflating setup errors and peers."""

    STARTED = "started"
    STARTED_WITHOUT_TRAY = "started_without_tray"
    SECONDARY_INSTANCE = "secondary_instance"
    SETUP_FAILED = "setup_failed"


def _start_shell_if_available(shell) -> ShellStartResult:
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
        # QtShell.start() has already removed any partially-created tray or
        # hotkey resources.  It deliberately keeps the instance guard owned so
        # this fallback runtime remains single-instance until shutdown.
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
    """Synchronize Settings and home preferences with explicit ownership.

    Settings edits remain a draft until its Save action. Home mode/language
    changes are the only bridge-originated updates and persist immediately.
    The guard prevents the Settings-to-bridge reflection from being mistaken
    for a new home edit and saving the draft recursively.
    """

    syncing_from_settings = False

    def sync_bridge_from_settings() -> None:
        nonlocal syncing_from_settings
        syncing_from_settings = True
        try:
            bridge.setMode(settings.mode)
            bridge.setLanguage(settings.language)
        finally:
            syncing_from_settings = False

    def persist_home_preference(setter, value) -> None:
        setter(value)
        if not syncing_from_settings:
            settings.save()

    settings.configChanged.connect(sync_bridge_from_settings)
    bridge.modeChanged.connect(
        lambda: persist_home_preference(settings.setMode, bridge.mode)
    )
    bridge.languageChanged.connect(
        lambda: persist_home_preference(settings.setLanguage, bridge.language)
    )


def _show_translation_picker_if_needed(bridge, shell) -> None:
    """Reveal a picker that was opened by a hotkey while the window was hidden."""

    if bridge.surface == "translation_picker":
        shell.show_window()


def _sync_recording_escape_hotkey(bridge, hotkeys) -> None:
    """Register global Escape only while the workflow is recording."""

    if hotkeys is not None:
        hotkeys.set_recording_active(bridge.surface == "recording")


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
    if hotkeys is not None:
        bridge.surfaceChanged.connect(
            lambda: _sync_recording_escape_hotkey(bridge, hotkeys)
        )
        _sync_recording_escape_hotkey(bridge, hotkeys)
    shell = QtShell(
        window,
        hotkeys=hotkeys,
        application=app,
        icon=_load_branding_icon(),
        parent=app,
    )
    shell.hotkeyTriggered.connect(bridge.handleHotkey)
    bridge.surfaceChanged.connect(
        lambda: _show_translation_picker_if_needed(bridge, shell)
    )
    _connect_shutdown(app, shell, runtime)

    shell_result = _start_shell_if_available(shell)
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
