"""Focused tests for the typed Qt Quick settings controller."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

try:
    from PySide6.QtCore import QCoreApplication
    from spikes.pyside6.qml_settings import QmlSettingsController

    PYSIDE6_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    PYSIDE6_AVAILABLE = False

from repositories import (
    AppConfig,
    ApplicationRepositories,
    LocalConfigRepository,
    LocalUsageStatsRepository,
)
from secret_store import MemorySecretStore
from workflow_config import WorkflowScope


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "spikes" / "pyside6" / "qml_settings.py"


def _repositories(directory: str) -> ApplicationRepositories:
    root = Path(directory)
    return ApplicationRepositories(
        config=LocalConfigRepository(
            root / "config.json",
            secret_store=MemorySecretStore(),
        ),
        usage_stats=LocalUsageStatsRepository(root / "usage_stats.json"),
    )


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional QML dependency")
class QmlSettingsControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QCoreApplication.instance() or QCoreApplication([])

    def test_load_exposes_typed_app_config_and_selected_route(self):
        with TemporaryDirectory() as directory:
            repositories = _repositories(directory)
            repositories.config.save(
                AppConfig.from_mapping(
                    {
                        "ui_mode": "transcription",
                        "ui_language": "pt",
                        "autostart": True,
                        "history_enabled": True,
                        "history_retention_days": 14,
                        "workflows": {
                            "rewrite": {
                                "provider_id": "groq",
                                "model_id": "llama-3.3-70b-versatile",
                                "prompt": "Rewrite selected text only.",
                                "custom_endpoint": "https://proxy.example/v1",
                                "enabled": True,
                            }
                        },
                    }
                )
            )

            controller = QmlSettingsController(repositories)

            self.assertEqual(controller.mode, "transcription")
            self.assertEqual(controller.language, "pt")
            self.assertTrue(controller.autostart)
            self.assertTrue(controller.historyEnabled)
            self.assertEqual(controller.historyRetentionDays, 14)
            self.assertEqual(
                controller.workflowScopes,
                [scope.value for scope in WorkflowScope],
            )
            self.assertEqual(controller.selectedScope, "transcription")
            self.assertEqual(
                controller.routeFor("rewrite"),
                {
                    "scope": "rewrite",
                    "providerId": "groq",
                    "modelId": "llama-3.3-70b-versatile",
                    "prompt": "Rewrite selected text only.",
                    "customEndpoint": "https://proxy.example/v1",
                    "enabled": True,
                },
            )

            self.assertTrue(controller.selectWorkflow("rewrite"))
            self.assertEqual(controller.routeProviderId, "groq")
            self.assertEqual(controller.routeModelId, "llama-3.3-70b-versatile")

    def test_properties_and_slots_update_one_typed_draft(self):
        with TemporaryDirectory() as directory:
            controller = QmlSettingsController(_repositories(directory))
            changes = []
            routes = []
            controller.configChanged.connect(lambda: changes.append(True))
            controller.routeChanged.connect(lambda: routes.append(True))

            self.assertTrue(controller.selectWorkflow("translation"))
            self.assertTrue(controller.setMode("transcription"))
            self.assertTrue(controller.setLanguage("de"))
            self.assertTrue(controller.setAutostart(True))
            self.assertTrue(controller.setHistoryEnabled(True))
            self.assertTrue(controller.setHistoryRetentionDays(None))
            self.assertTrue(
                controller.setRoute(
                    "translation",
                    "openai",
                    "gpt-4o-mini",
                    "Translate literally.",
                    "https://proxy.example/v1",
                    True,
                )
            )

            self.assertTrue(controller.dirty)
            self.assertEqual(controller.mode, "transcription")
            self.assertEqual(controller.language, "de")
            self.assertTrue(controller.autostart)
            self.assertTrue(controller.historyEnabled)
            self.assertIsNone(controller.historyRetentionDays)
            self.assertGreaterEqual(len(changes), 5)
            self.assertGreaterEqual(len(routes), 2)
            self.assertFalse(controller.setMode("not-a-mode"))
            self.assertIn("Invalid mode", controller.lastError)
            self.assertFalse(controller.setHistoryRetentionDays(-1))
            self.assertIn("cannot be negative", controller.lastError)

    def test_every_route_edit_marks_the_route_independent(self):
        edit_cases = (
            ("setRouteProviderId", ("groq",)),
            ("setRouteModelId", ("custom-model",)),
            ("setRoutePrompt", ("Rewrite only.",)),
            ("setRouteCustomEndpoint", ("https://proxy.example/v1",)),
            ("setRouteEnabled", (False,)),
            (
                "setRoute",
                (
                    "rewrite",
                    "groq",
                    "llama-3.3-70b-versatile",
                    "Rewrite only.",
                    "",
                    True,
                ),
            ),
        )

        for method_name, arguments in edit_cases:
            with self.subTest(method=method_name):
                with TemporaryDirectory() as directory:
                    controller = QmlSettingsController(_repositories(directory))
                    self.assertTrue(controller.selectWorkflow("rewrite"))
                    self.assertFalse(controller._config.workflow("rewrite").independent)

                    self.assertTrue(getattr(controller, method_name)(*arguments))
                    self.assertTrue(controller._config.workflow("rewrite").independent)

    def test_invalid_route_queries_set_error_and_return_qml_safe_values(self):
        with TemporaryDirectory() as directory:
            controller = QmlSettingsController(_repositories(directory))

            self.assertEqual(controller.routeFor("not-a-scope"), {})
            self.assertIn("Unknown workflow scope", controller.lastError)

            self.assertEqual(controller.providersForScope("not-a-scope"), [])
            self.assertIn("Unknown workflow scope", controller.lastError)

            self.assertEqual(controller.routeFor("rewrite")["scope"], "rewrite")
            self.assertEqual(controller.lastError, "")

    def test_valid_unchanged_edit_clears_a_previous_error(self):
        with TemporaryDirectory() as directory:
            controller = QmlSettingsController(_repositories(directory))

            self.assertFalse(controller.setMode("not-a-mode"))
            self.assertNotEqual(controller.lastError, "")

            self.assertTrue(controller.setMode(controller.mode))
            self.assertEqual(controller.lastError, "")

    def test_history_retention_rejects_non_integer_float_without_truncating(self):
        with TemporaryDirectory() as directory:
            controller = QmlSettingsController(_repositories(directory))

            self.assertFalse(controller.setHistoryRetentionDays(14.9))
            self.assertIn("must be an integer", controller.lastError)
            self.assertEqual(controller.historyRetentionDays, 30)

            self.assertTrue(controller.setHistoryRetentionDays(14.0))
            self.assertEqual(controller.historyRetentionDays, 14)

    def test_save_delegates_to_atomic_repository_apply_and_reloads_canonical_config(
        self,
    ):
        with TemporaryDirectory() as directory:
            repositories = _repositories(directory)
            controller = QmlSettingsController(repositories)
            controller.selectWorkflow("rewrite")
            controller.setRouteProviderId("groq")
            controller.setRouteModelId("llama-3.3-70b-versatile")
            controller.setRoutePrompt("Rewrite only.")

            with patch.object(
                repositories.config,
                "apply",
                wraps=repositories.config.apply,
            ) as apply:
                self.assertTrue(controller.save())

            apply.assert_called_once()
            self.assertFalse(controller.dirty)
            self.assertEqual(controller.lastError, "")
            persisted = repositories.config.load()
            self.assertEqual(
                persisted.workflow(WorkflowScope.REWRITE).provider_id,
                "groq",
            )
            self.assertEqual(
                persisted.workflow(WorkflowScope.REWRITE).model_id,
                "llama-3.3-70b-versatile",
            )
            self.assertTrue(persisted.workflow(WorkflowScope.REWRITE).independent)

            payload = json.loads(
                (Path(directory) / "config.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["workflows"]["rewrite"]["provider_id"], "groq")

    def test_failed_atomic_apply_keeps_the_draft_and_does_not_write(self):
        with TemporaryDirectory() as directory:
            repositories = _repositories(directory)
            controller = QmlSettingsController(repositories)
            config_path = Path(directory) / "config.json"
            before = config_path.read_bytes() if config_path.exists() else None

            controller.selectWorkflow("rewrite")
            controller.setRouteCustomEndpoint("https://proxy.example:0/v1")

            self.assertFalse(controller.save())
            self.assertTrue(controller.dirty)
            self.assertIn("port", controller.lastError.lower())
            self.assertEqual(
                config_path.read_bytes() if config_path.exists() else None,
                before,
            )
            self.assertEqual(
                controller.routeCustomEndpoint,
                "https://proxy.example:0/v1",
            )

    def test_load_replaces_the_draft_without_a_ui_or_legacy_import(self):
        with TemporaryDirectory() as directory:
            repositories = _repositories(directory)
            controller = QmlSettingsController(repositories)
            controller.setLanguage("es")
            self.assertTrue(controller.dirty)

            repositories.config.save(AppConfig.from_mapping({"ui_language": "ru"}))
            self.assertTrue(controller.load())

            self.assertEqual(controller.language, "ru")
            self.assertFalse(controller.dirty)

    def test_qml_properties_are_registered_and_module_does_not_import_app(self):
        property_names = {
            controller_property.name()
            for controller_property in (
                QmlSettingsController.staticMetaObject.property(index)
                for index in range(
                    QmlSettingsController.staticMetaObject.propertyCount()
                )
            )
        }
        self.assertTrue(
            {
                "mode",
                "language",
                "selectedScope",
                "routeProviderId",
                "routeModelId",
                "routePrompt",
                "routeCustomEndpoint",
                "routeEnabled",
                "dirty",
                "lastError",
            }.issubset(property_names)
        )

        source = SETTINGS.read_text(encoding="utf-8")
        tree = ast.parse(source)
        top_level_imports = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level_imports.update(
                    alias.name.split(".")[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level_imports.add(node.module.split(".")[0])
        self.assertNotIn("app", top_level_imports)
        self.assertNotIn("customtkinter", top_level_imports)
        self.assertNotIn("tkinter", top_level_imports)

        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "from spikes.pyside6.qml_settings import "
                    "QmlSettingsController; "
                    "print('app' in sys.modules)"
                ),
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()
