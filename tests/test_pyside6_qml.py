import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPIKE = ROOT / "spikes" / "pyside6"
QML_ROOT = SPIKE / "qml"


class PySide6QmlPilotTests(unittest.TestCase):
    def test_qml_pilot_entrypoint_and_assets_are_present(self):
        entrypoint = SPIKE / "qml_app.py"
        self.assertTrue(entrypoint.is_file())

        qml_files = sorted(QML_ROOT.rglob("*.qml"))
        self.assertTrue(qml_files, "the QML pilot must contain at least one QML file")
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
        self.assertIn("Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint", main_source)
        self.assertIn("Layout.preferredWidth: 32", main_source)
        self.assertIn("Layout.preferredWidth: 78", main_source)
        self.assertIn("Layout.preferredWidth: 48", main_source)
        self.assertIn("Layout.preferredWidth: 26", main_source)
        self.assertNotIn("#72a7ff", qml_source)
        self.assertNotIn("#4f83e8", qml_source)
        self.assertIn("property string languageCode: \"EN\"", main_source)
        self.assertIn("text: languageCode", main_source)
        self.assertIn('Accessible.name: "Language: "', main_source)
        self.assertIn('languageCode === "EN"', main_source)
        self.assertIn("property bool successVisible: false", status_pill_source)
        self.assertIn("interval: 850", status_pill_source)
        self.assertIn(
            'workflow.surface === "success" && successVisible', status_pill_source)
        self.assertIn('resultPage.copyLabel = "Copy"', main_source)

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
        self.assertIn("QmlWorkflowBridge(app)", source)


if __name__ == "__main__":
    unittest.main()
