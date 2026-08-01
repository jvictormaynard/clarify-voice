import ast
import unittest
from pathlib import Path

from spikes.pyside6.model import FakeWorkflow, Surface


ROOT = Path(__file__).resolve().parents[1]
SPIKE = ROOT / "spikes" / "pyside6"


class PySide6SpikeTests(unittest.TestCase):
    def test_fake_workflow_covers_the_bounded_surface_flow(self):
        workflow = FakeWorkflow()
        workflow = workflow.transition("record")
        self.assertEqual(workflow.surface, Surface.RECORDING)
        workflow = workflow.transition("process").transition("complete")
        self.assertEqual(workflow.surface, Surface.SUCCESS)
        workflow = workflow.transition("show_result")
        self.assertEqual(workflow.surface, Surface.RESULT)
        workflow = workflow.transition("reset").open_settings()
        self.assertEqual(workflow.surface, Surface.SETTINGS)
        self.assertIn("provider", workflow.result)

    def test_spike_isolated_from_production_modules(self):
        source = (SPIKE / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
            for alias in node.names
        )
        self.assertNotIn("app", imported)
        self.assertNotIn("desktop_state", imported)
        self.assertNotIn("windows_hotkeys", imported)
        self.assertNotIn("requests", imported)
        self.assertNotIn("sounddevice", imported)

    def test_spike_entry_points_and_docs_are_present(self):
        for relative in (
            "__init__.py",
            "app.py",
            "model.py",
            "README.md",
            "requirements.txt",
            "package.ps1",
            "benchmark.ps1",
            ".gitignore",
        ):
            self.assertTrue((SPIKE / relative).is_file(), relative)
        decision = (ROOT / "docs" / "pyside6-decision.md").read_text(encoding="utf-8")
        for phrase in (
            "provisional defer",
            "Licensing and redistribution",
            "Migration and rollback outline",
            "Recommendation",
            "pending Windows run",
        ):
            self.assertIn(phrase, decision)

    def test_spike_packaging_does_not_use_production_output_paths(self):
        package = (SPIKE / "package.ps1").read_text(encoding="utf-8")
        self.assertIn('"artifacts"', package)
        self.assertNotIn('"dist"', package)
        self.assertNotIn("scripts\\build.ps1", package)

    def test_production_requirements_do_not_gain_optional_qt_dependency(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("PySide6", requirements)
        self.assertNotIn("pyside6", requirements)


if __name__ == "__main__":
    unittest.main()
