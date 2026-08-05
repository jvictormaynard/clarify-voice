import ast
import unittest
from pathlib import Path


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
        self.assertIn('property string languageCode: "EN"', main_source)
        self.assertIn("text: languageCode", main_source)
        self.assertIn('Accessible.name: "Language: "', main_source)
        self.assertIn('languageCode === "EN"', main_source)
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
        self.assertIn("onVisibleChanged: resetCopyConfirmation()", main_source)
        self.assertIn('resultPage.copyLabel = "Copy"', main_source)
        self.assertIn("workflow.stopRecording()", main_source)
        self.assertIn("workflow.cancelRecording()", main_source)
        self.assertIn("workflow.setLanguage", main_source)
        self.assertIn("workflow.setMode", main_source)
        self.assertNotIn('sequence: "Alt+L"', main_source)

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
        self.assertIn("QGuiApplication", source)
        self.assertIn("create_real_workflow_service", source)
        self.assertIn("QmlWorkflowBridge(", source)
        self.assertNotIn("FakeWorkflow", source)
        self.assertNotIn("--fake", source)

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
            "QtClipboardGateway",
            "QtStatisticsGateway",
        ):
            self.assertIn(f"class {symbol}", runtime_source)
        self.assertIn("PROVIDER_REGISTRY", runtime_source)
        self.assertIn("WindowsClipboardAdapter", runtime_source)
        self.assertNotIn("import app", runtime_source)
        self.assertNotIn("to_legacy_mapping", runtime_source)

    def test_real_entrypoint_has_no_fake_or_legacy_runtime(self):
        source = (SPIKE / "qml_app.py").read_text(encoding="utf-8")
        runtime_source = (SPIKE / "qml_runtime.py").read_text(encoding="utf-8")
        self.assertIn("QtRuntimeError", source)
        self.assertNotIn("FakeWorkflow", source)
        self.assertNotIn("legacy_adapters", runtime_source)
        self.assertNotIn("QmlRuntimeUnavailableError", runtime_source)


if __name__ == "__main__":
    unittest.main()
