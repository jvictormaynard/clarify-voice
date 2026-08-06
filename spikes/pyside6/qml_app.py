"""Real Qt Quick/QML entrypoint for ClarifyVoice dictation."""

from __future__ import annotations

import sys
from enum import Enum
from functools import partial
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
        if root in seen:
            continue
        seen.add(root)
        candidate = root / relative_path
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


def _qml_root() -> Path:
    """Resolve the QML asset directory for source and frozen launches."""

    roots: list[Path] = []
    bundled_root = getattr(sys, "_MEIPASS", None)
    if bundled_root:
        roots.append(Path(bundled_root))
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    roots.extend(
        (Path(__file__).resolve().parent, _REPOSITORY_ROOT / "spikes" / "pyside6")
    )

    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        candidate = root / "qml"
        if (candidate / "Main.qml").is_file():
            return candidate

    raise FileNotFoundError(
        f"ClarifyVoice QML assets were not found under: {', '.join(map(str, roots))}"
    )


try:
    from .qml_audio_batch import QmlAudioFileImportController  # noqa: E402
    from .qml_bridge import QmlWorkflowBridge  # noqa: E402
    from .qml_settings import QmlSettingsController  # noqa: E402
    from .qml_voice_translation import (  # noqa: E402
        create_qml_voice_translation_controller,
    )
    from .qml_runtime import (  # noqa: E402
        QtRuntimeError,
        QtStatisticsGateway,
        QtWorkflowScheduler,
        create_real_workflow_runtime,
    )
    from .qt_shell import QtShell, WindowsGlobalHotkeyBackend  # noqa: E402
except ImportError:  # PyInstaller analyzes this file as a standalone entry point.
    from qml_audio_batch import QmlAudioFileImportController  # noqa: E402
    from qml_bridge import QmlWorkflowBridge  # noqa: E402
    from qml_settings import QmlSettingsController  # noqa: E402
    from qml_voice_translation import (  # noqa: E402
        create_qml_voice_translation_controller,
    )
    from qml_runtime import (  # noqa: E402
        QtRuntimeError,
        QtStatisticsGateway,
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


def _register_qml_context(
    engine,
    workflow,
    settings,
    voice_translation=None,
    audio_batch=None,
) -> None:
    """Expose the real Qt-facing controllers before QML is loaded."""

    context = engine.rootContext()
    context.setContextProperty("workflow", workflow)
    context.setContextProperty("settings", settings)
    if voice_translation is not None:
        context.setContextProperty("voiceTranslation", voice_translation)
    if audio_batch is not None:
        context.setContextProperty("audioBatch", audio_batch)


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

    def persist_home_preference(setter, persister, value) -> None:
        if not setter(value):
            return
        if not syncing_from_settings:
            persister(value)

    settings.configChanged.connect(sync_bridge_from_settings)
    bridge.modeChanged.connect(
        lambda: persist_home_preference(
            settings.setMode,
            settings.persistMode,
            bridge.mode,
        )
    )
    bridge.languageChanged.connect(
        lambda: persist_home_preference(
            settings.setLanguage,
            settings.persistLanguage,
            bridge.language,
        )
    )


def _hidden_start_requested(arguments: list[str]) -> bool:
    """Accept the supported background launch mode and reject other flags."""

    if not arguments:
        return False
    if arguments == ["--hidden"]:
        return True
    raise ValueError("unsupported launch arguments")


def _show_translation_picker_if_needed(bridge, shell) -> None:
    """Reveal a picker that was opened by a hotkey while the window was hidden."""

    if bridge.surface == "translation_picker":
        shell.show_window()


def _sync_recording_escape_hotkey(bridge, hotkeys) -> None:
    """Register global Escape only while the workflow is recording."""

    if hotkeys is not None:
        hotkeys.set_recording_active(bridge.surface == "recording")


def _connect_shutdown(
    app,
    shell,
    runtime,
    voice_translation=None,
    audio_batch=None,
) -> None:
    """Stop shell-owned native resources before the workflow runtime."""

    if voice_translation is not None:
        app.aboutToQuit.connect(voice_translation.cancel)
    if audio_batch is not None:
        app.aboutToQuit.connect(audio_batch.cancel)
    app.aboutToQuit.connect(shell.stop)
    app.aboutToQuit.connect(runtime.shutdown)


def _record_voice_translation_usage(
    statistics,
    config,
    state,
    duration_seconds,
) -> None:
    """Record both voice-translation legs through the QML stats boundary."""

    source = str(getattr(state, "raw_transcript", "") or "")
    statistics.record_dictation(
        {
            "provider": str(getattr(state, "transcription_provider", "") or ""),
            "model": str(getattr(state, "transcription_model", "") or ""),
            "mode": "voice_translation",
        },
        duration_seconds,
        source,
    )
    route = getattr(config, "route", None)
    statistics.record_translation(
        str(getattr(route, "provider_id", "") or ""),
        str(getattr(route, "model_id", "") or ""),
        source,
        str(getattr(state, "translated_text", "") or ""),
        str(getattr(config, "target_language", "") or ""),
    )


def main(argv: list[str] | None = None) -> int:
    """Start the real QML frontend; missing runtime dependencies are fatal."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        start_hidden = _hidden_start_requested(arguments)
    except ValueError:
        print(
            "ClarifyVoice QML accepts only the optional --hidden launch flag.",
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
    usage_statistics = QtStatisticsGateway(repositories)
    voice_translation = create_qml_voice_translation_controller(
        repositories.config.load,
        runtime.recording_audio,
        runtime.clipboard,
        scheduler,
        on_usage=partial(_record_voice_translation_usage, usage_statistics),
        parent=app,
    )
    audio_batch = QmlAudioFileImportController(
        runtime.audio_batch_service,
        selection_factory=runtime.audio_file_selection,
        scheduler=scheduler,
        copy_runner=runtime.copy_result,
        dispatch_runner=scheduler.run_dispatch,
        parent=app,
    )

    def toggle_voice_translation() -> bool:
        """Toggle the dedicated voice route from the global hotkey."""

        if workflow_service.state.phase.value != "ready":
            return False
        if voice_translation.active:
            return (
                voice_translation.stop()
                if voice_translation.phase == "recording"
                else voice_translation.cancel()
            )
        target = runtime.clipboard.capture_target()
        if target is None:
            return False
        return voice_translation.startForTarget(target)

    bridge = QmlWorkflowBridge(
        workflow_service,
        app_config=loaded_config,
        dispatch_runner=scheduler.run_dispatch,
        copy_runner=runtime.copy_result,
        voice_translation_handler=toggle_voice_translation,
        voice_translation_controller=voice_translation,
        audio_batch_controller=audio_batch,
        parent=app,
    )
    hotkeys = None

    def apply_qml_hotkeys(settings) -> None:
        if hotkeys is not None:
            hotkeys.reconfigure(settings)

    settings = QmlSettingsController(
        repositories,
        parent=app,
        microphone_backend=runtime.recording_audio.recorder,
        hotkey_applier=apply_qml_hotkeys,
    )
    _connect_preference_sync(bridge, settings)
    engine = QQmlApplicationEngine()
    _register_qml_context(
        engine,
        bridge,
        settings,
        voice_translation,
        audio_batch,
    )

    qml_root = _qml_root()
    engine.addImportPath(str(qml_root))
    engine.load(QUrl.fromLocalFile(str(qml_root / "Main.qml")))
    if not engine.rootObjects():
        runtime.shutdown()
        return 1

    window = engine.rootObjects()[0]
    if start_hidden:
        window.hide()
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
    _connect_shutdown(app, shell, runtime, voice_translation, audio_batch)
    app.aboutToQuit.connect(settings.shutdown)

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
