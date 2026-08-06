import ast
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from PySide6.QtCore import QObject, QTimer, Signal
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtWidgets import QApplication
    from spikes.pyside6.qml_app import (
        ShellStartResult,
        _connect_preference_sync,
        _connect_shutdown,
        _register_qml_context,
        _show_translation_picker_if_needed,
        _start_shell_if_available,
    )
    from spikes.pyside6.qml_bridge import QmlWorkflowBridge
    from spikes.pyside6.qml_settings import QmlSettingsController
except (ImportError, ModuleNotFoundError):
    PYSIDE6_AVAILABLE = False
else:
    PYSIDE6_AVAILABLE = True


ROOT = Path(__file__).resolve().parents[1]
SPIKE = ROOT / "spikes" / "pyside6"
QML_ROOT = SPIKE / "qml"


class PySide6QmlFrontendTests(unittest.TestCase):
    def test_qml_entrypoint_and_assets_are_present(self):
        entrypoint = SPIKE / "qml_app.py"
        self.assertTrue(entrypoint.is_file())

        qml_files = sorted(QML_ROOT.rglob("*.qml"))
        self.assertTrue(qml_files, "the QML frontend must contain QML assets")
        main_files = [path for path in qml_files if path.name.casefold() == "main.qml"]
        self.assertEqual(main_files, [QML_ROOT / "Main.qml"])
        qml_source = "\n".join(path.read_text(encoding="utf-8") for path in qml_files)

        self.assertIn("ApplicationWindow", qml_source)
        self.assertIn("State", qml_source)
        self.assertIn("Transition", qml_source)
        self.assertRegex(qml_source, r"(Behavior|NumberAnimation|OpacityAnimator)")
        self.assertRegex(qml_source, r"Accessible\.(name|description)")
        self.assertIn("Accessible.name: workflow.status", qml_source)
        self.assertIn("Accessible.name: workflow.result", qml_source)
        self.assertNotIn('Accessible.name: "Prototype result text"', qml_source)

        theme_source = (QML_ROOT / "Theme.qml").read_text(encoding="utf-8")
        for value in ("#0a0a0a", "#050505", "#1c1c1c", "#ffffff", "#666666"):
            self.assertIn(value, theme_source)
        self.assertIn("readonly property int windowWidth: 380", theme_source)
        self.assertIn("readonly property int windowHeight: 48", theme_source)

        main_source = (QML_ROOT / "Main.qml").read_text(encoding="utf-8")
        status_pill_source = (QML_ROOT / "StatusPill.qml").read_text(encoding="utf-8")
        self.assertIn('color: "transparent"', main_source)
        self.assertIn(
            "Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint", main_source
        )
        self.assertIn("Layout.preferredWidth: 32", main_source)
        self.assertIn("Layout.preferredWidth: 78", main_source)
        self.assertIn("Layout.preferredWidth: 48", main_source)
        self.assertIn("Layout.preferredWidth: 26", main_source)
        self.assertNotIn("#72a7ff", qml_source)
        self.assertNotIn("#4f83e8", qml_source)
        self.assertIn("text: languageCode", main_source)
        self.assertIn('Accessible.name: "Language: "', main_source)
        self.assertIn(
            "readonly property var supportedLanguages: [\n"
            '                                "en", "pt", "es", "de", "ru"\n'
            "                            ]",
            main_source,
        )
        for language, name in (
            ("en", "English"),
            ("pt", "Portuguese"),
            ("es", "Spanish"),
            ("de", "German"),
            ("ru", "Russian"),
        ):
            self.assertIn(f'"{language}": "{name}"', main_source)
        self.assertIn(
            "var currentIndex = supportedLanguages.indexOf(workflow.language)",
            main_source,
        )
        self.assertIn(
            "var nextIndex = (currentIndex + 1) % supportedLanguages.length",
            main_source,
        )
        self.assertNotIn('languageCode === "EN"', main_source)
        self.assertIn(
            "property string languageCode: workflow.language.toUpperCase()",
            main_source,
        )
        self.assertIn(
            'property bool promptMode: workflow.mode === "prompt"', main_source
        )
        self.assertNotIn("languageCode = languageCode ===", main_source)
        self.assertNotIn("homePage.promptMode = !homePage.promptMode", main_source)
        self.assertIn('Accessible.name: "Mode: "', main_source)
        self.assertIn("root.startSystemMove()", main_source)
        self.assertIn("DragHandler", main_source)
        self.assertIn("property bool successVisible: false", status_pill_source)
        self.assertIn("interval: 850", status_pill_source)
        self.assertIn(
            'workflow.surface === "success" && successVisible', status_pill_source
        )
        self.assertIn("copyResetTimer", main_source)
        self.assertIn("copyResetTimer.restart()", main_source)
        self.assertIn("function onCopyCompleted(success)", main_source)
        self.assertIn("onClicked: workflow.copyResult()", main_source)
        self.assertIn("onVisibleChanged: resetCopyConfirmation()", main_source)
        self.assertIn('resultPage.copyLabel = "Copy"', main_source)
        self.assertIn("workflow.stopRecording()", main_source)
        self.assertIn("workflow.cancelRecording()", main_source)
        self.assertIn('workflow.surface === "error"', main_source)
        self.assertIn("Dismiss workflow error", main_source)
        self.assertIn("workflow.reset()", main_source)
        self.assertIn("workflow.setLanguage", main_source)
        self.assertIn("workflow.setMode", main_source)
        self.assertIn("workflow.copyResult()", main_source)
        self.assertNotIn("Global shortcuts and settings will be connected", main_source)
        self.assertIn('objectName: "settingsPage"', main_source)
        for binding in (
            "settings.mode",
            "settings.language",
            "settings.autostart",
            "settings.historyEnabled",
            "settings.historyRetentionDays",
            "settings.selectedScope",
            "settings.routeProviderId",
            "settings.routeModelId",
            "settings.routePrompt",
            "settings.routeCustomEndpoint",
            "settings.routeEnabled",
            "settings.lastError",
            "settings.dirty",
            "settings.load()",
            "settings.save()",
        ):
            self.assertIn(binding, main_source)
        self.assertIn('workflow.surface === "translation_picker"', main_source)
        self.assertIn('objectName: "translationPickerPage"', main_source)
        self.assertIn("workflow.translationOptions", main_source)
        self.assertIn("workflow.chooseTranslation(modelData.code)", main_source)
        self.assertIn("workflow.cancelTranslation()", main_source)
        self.assertNotIn("Alt+L", main_source)

    def test_qml_entrypoint_uses_qt_quick_and_stays_production_isolated(self):
        source = (SPIKE / "qml_app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])

        self.assertIn("PySide6", imported_roots)
        self.assertNotIn("app", imported_roots)
        self.assertNotIn("provider_adapters", imported_roots)
        self.assertNotIn("provider_registry", imported_roots)
        self.assertNotIn("requests", imported_roots)
        self.assertNotIn("sounddevice", imported_roots)
        self.assertNotIn("windows_hotkeys", imported_roots)
        self.assertNotIn("windows_clipboard", imported_roots)
        self.assertIn("QQmlApplicationEngine", source)
        self.assertIn("QApplication", source)
        self.assertNotIn("QGuiApplication", source)
        self.assertIn("QSystemTrayIcon", source)
        self.assertIn("create_real_workflow_runtime", source)
        self.assertIn("QmlSettingsController", source)
        self.assertIn("loaded_config = repositories.config.load()", source)
        self.assertIn("app_config=loaded_config", source)
        self.assertIn('setContextProperty("settings", settings)', source)
        self.assertIn("runtime.repositories", source)
        self.assertIn("QtShell", source)
        self.assertIn("WindowsGlobalHotkeyBackend", source)
        self.assertIn('if sys.platform == "win32"', source)
        self.assertIn("shell.hotkeyTriggered.connect(bridge.handleHotkey)", source)
        self.assertIn("_connect_preference_sync(bridge, settings)", source)
        self.assertIn("settings.configChanged.connect", source)
        self.assertIn("bridge.modeChanged.connect", source)
        self.assertIn("bridge.languageChanged.connect", source)
        self.assertIn("_show_translation_picker_if_needed", source)
        self.assertIn("tray_available=tray_available", source)
        self.assertIn("ShellStartResult.SECONDARY_INSTANCE", source)
        self.assertIn("QmlWorkflowBridge(", source)
        self.assertIn("dispatch_runner=scheduler.run_dispatch", source)
        self.assertIn("copy_runner=runtime.copy_result", source)
        self.assertIn("app.aboutToQuit.connect(shell.stop)", source)
        self.assertIn("app.aboutToQuit.connect(runtime.shutdown)", source)
        self.assertLess(
            source.index("app.aboutToQuit.connect(shell.stop)"),
            source.index("app.aboutToQuit.connect(runtime.shutdown)"),
        )
        self.assertLess(source.index("engine.rootObjects()"), source.index("QtShell("))
        self.assertIn("_start_shell_if_available", source)
        self.assertNotIn("FakeWorkflow", source)
        self.assertNotIn("--fake", source)

    def test_qml_bridge_hydrates_persisted_preferences(self):
        bridge_source = (SPIKE / "qml_bridge.py").read_text(encoding="utf-8")
        self.assertIn("app_config: Any | None = None", bridge_source)
        self.assertIn("saved_config = current_config()", bridge_source)
        self.assertIn('getattr(ui_preferences, "mode", "prompt")', bridge_source)
        self.assertIn('getattr(ui_preferences, "language", "en")', bridge_source)
        self.assertIn("def mode(self) -> str:", bridge_source)
        self.assertIn("def language(self) -> str:", bridge_source)
        self.assertIn("StartDictation(None, self._mode, self._language)", bridge_source)

        main_source = (QML_ROOT / "Main.qml").read_text(encoding="utf-8")
        self.assertIn(
            'property bool promptMode: workflow.mode === "prompt"', main_source
        )
        self.assertIn(
            "property string languageCode: workflow.language.toUpperCase()",
            main_source,
        )
        self.assertNotIn("languageCode = languageCode ===", main_source)
        self.assertNotIn("homePage.promptMode = !homePage.promptMode", main_source)

    def test_qml_runtime_uses_ui_free_adapters(self):
        for filename in ("qml_bridge.py", "qml_runtime.py"):
            source = (SPIKE / filename).read_text(encoding="utf-8")
            tree = ast.parse(source)
            top_level_imports = set()
            for node in tree.body:
                if isinstance(node, ast.Import):
                    top_level_imports.update(
                        alias.name.split(".")[0] for alias in node.names
                    )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    top_level_imports.add(node.module.split(".")[0])
            self.assertNotIn("app", top_level_imports, filename)
            self.assertNotIn("customtkinter", top_level_imports, filename)
            self.assertNotIn("tkinter", top_level_imports, filename)

        runtime_source = (SPIKE / "qml_runtime.py").read_text(encoding="utf-8")
        for symbol in (
            "QtProviderGateway",
            "QtRecordingAudioGateway",
            "QtWorkflowRuntime",
            "QtClipboardGateway",
            "QtStatisticsGateway",
        ):
            self.assertIn(f"class {symbol}", runtime_source)
        self.assertIn("self.repositories = repositories", runtime_source)
        self.assertIn("repositories=active", runtime_source)
        self.assertIn("PROVIDER_REGISTRY", runtime_source)
        self.assertIn("WindowsClipboardAdapter", runtime_source)
        self.assertNotIn("import app", runtime_source)
        self.assertNotIn("to_legacy_mapping", runtime_source)

    def test_qml_bridge_routes_native_hotkeys_to_real_commands(self):
        bridge_source = (SPIKE / "qml_bridge.py").read_text(encoding="utf-8")
        self.assertIn("def handleHotkey(self, action: str) -> bool:", bridge_source)
        self.assertIn('normalized == "recording_hotkey"', bridge_source)
        self.assertIn('normalized == "rewrite_hotkey"', bridge_source)
        self.assertIn('normalized == "translation_hotkey"', bridge_source)
        self.assertIn("StartRewrite()", bridge_source)
        self.assertIn("StartTranslation()", bridge_source)
        self.assertIn("CancelTranslation()", bridge_source)
        self.assertIn("ChooseTranslationLanguage", bridge_source)
        self.assertIn(
            "def chooseTranslation(self, language: str) -> bool:", bridge_source
        )
        self.assertIn("def cancelTranslation(self) -> bool:", bridge_source)
        self.assertIn(
            "def translationOptions(self) -> list[dict[str, str]]:", bridge_source
        )
        self.assertIn('return "translation_picker"', bridge_source)

    def test_real_entrypoint_has_no_fake_or_legacy_runtime(self):
        source = (SPIKE / "qml_app.py").read_text(encoding="utf-8")
        runtime_source = (SPIKE / "qml_runtime.py").read_text(encoding="utf-8")
        self.assertIn("QtRuntimeError", source)
        self.assertNotIn("FakeWorkflow", source)
        self.assertNotIn("legacy_adapters", runtime_source)
        self.assertNotIn("QmlRuntimeUnavailableError", runtime_source)


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional QML dependency")
class QmlEntrypointIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_uses_qapplication_and_registers_real_context_properties(self):
        self.assertIsInstance(self.app, QApplication)
        engine = QQmlApplicationEngine()
        workflow = QObject()
        settings = QObject()

        _register_qml_context(engine, workflow, settings)

        self.assertIs(engine.rootContext().contextProperty("workflow"), workflow)
        self.assertIs(engine.rootContext().contextProperty("settings"), settings)

    def test_shutdown_connections_stop_shell_before_runtime(self):
        events = []

        class Shell(QObject):
            def stop(self):
                events.append("shell")

        class Runtime:
            def shutdown(self):
                events.append("runtime")

        shell = Shell()
        runtime = Runtime()
        _connect_shutdown(self.app, shell, runtime)
        QTimer.singleShot(0, self.app.quit)
        self.app.exec()

        self.assertEqual(events[-2:], ["shell", "runtime"])

    def test_preferences_sync_in_both_directions_and_persists_compact_edits(self):
        from repositories import AppConfig
        from workflows import StartDictation, WorkflowState

        class ConfigRepository:
            def __init__(self, config):
                self.config = config

            def load(self):
                return self.config

            def apply(self, config):
                self.config = config
                return config

        class Repositories:
            def __init__(self, config):
                self.config = config

        class WorkflowService:
            def __init__(self):
                self.state = WorkflowState()
                self.listeners = []
                self.commands = []

            def subscribe(self, listener):
                self.listeners.append(listener)

            def dispatch(self, command):
                self.commands.append(command)
                return True

        initial = AppConfig.from_mapping({"ui_mode": "prompt", "ui_language": "en"})
        config_repository = ConfigRepository(initial)
        settings = QmlSettingsController(Repositories(config_repository))
        service = WorkflowService()
        bridge = QmlWorkflowBridge(service, app_config=initial)
        _connect_preference_sync(bridge, settings)

        settings.setMode("transcription")
        settings.setLanguage("pt")
        bridge.startRecording()

        self.assertIsInstance(service.commands[-1], StartDictation)
        self.assertEqual(service.commands[-1].mode, "transcription")
        self.assertEqual(service.commands[-1].language, "pt")

        bridge.setMode("prompt")
        bridge.setLanguage("de")
        self.assertEqual(settings.mode, "prompt")
        self.assertEqual(settings.language, "de")
        self.assertTrue(settings.dirty)
        self.assertTrue(settings.save())
        persisted = config_repository.load()
        self.assertEqual(persisted.ui.mode, "prompt")
        self.assertEqual(persisted.ui.language, "de")

    def test_translation_picker_reveals_a_window_hidden_by_the_tray(self):
        class Bridge(QObject):
            surfaceChanged = Signal()

            def __init__(self):
                super().__init__()
                self.surface = "idle"

        class Shell:
            def __init__(self):
                self.show_calls = 0

            def show_window(self):
                self.show_calls += 1

        bridge = Bridge()
        shell = Shell()
        bridge.surfaceChanged.connect(
            lambda: _show_translation_picker_if_needed(bridge, shell)
        )

        bridge.surface = "translation_picker"
        bridge.surfaceChanged.emit()

        self.assertEqual(shell.show_calls, 1)

    def test_shell_failure_is_distinguished_from_secondary_and_tray_absence(self):
        class Shell:
            def __init__(self):
                self.stop_calls = 0
                self.start_calls = []

            def start(self, *, tray_available):
                self.start_calls.append(tray_available)
                raise RuntimeError("event filter failed")

            def stop(self):
                self.stop_calls += 1

        class Hotkeys:
            def __init__(self):
                self.stop_calls = 0

            def stop(self):
                self.stop_calls += 1

        shell = Shell()
        hotkeys = Hotkeys()
        with patch(
            "spikes.pyside6.qml_app.QSystemTrayIcon.isSystemTrayAvailable",
            return_value=True,
        ):
            result = _start_shell_if_available(shell, hotkeys)
        self.assertIs(result, ShellStartResult.SETUP_FAILED)
        self.assertEqual(shell.stop_calls, 1)
        self.assertEqual(hotkeys.stop_calls, 1)

        class NoTrayShell:
            def __init__(self):
                self.start_calls = []

            def start(self, *, tray_available):
                self.start_calls.append(tray_available)
                return True

        shell = NoTrayShell()
        with patch(
            "spikes.pyside6.qml_app.QSystemTrayIcon.isSystemTrayAvailable",
            return_value=False,
        ):
            result = _start_shell_if_available(shell, hotkeys)
        self.assertIs(result, ShellStartResult.STARTED_WITHOUT_TRAY)
        self.assertEqual(shell.start_calls, [False])

        class SecondaryShell:
            def start(self, *, tray_available):
                return False

        with patch(
            "spikes.pyside6.qml_app.QSystemTrayIcon.isSystemTrayAvailable",
            return_value=True,
        ):
            result = _start_shell_if_available(SecondaryShell(), None)
        self.assertIs(result, ShellStartResult.SECONDARY_INSTANCE)


if __name__ == "__main__":
    unittest.main()
