"""Focused tests for the typed Qt Quick settings controller."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

try:
    from PySide6.QtCore import QCoreApplication
    from spikes.pyside6 import qml_settings
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
from provider_types import ModelCatalog
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


class _RegistryKey:
    def __init__(self, registry):
        self.registry = registry

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Registry:
    HKEY_CURRENT_USER = 1
    REG_SZ = 1

    def __init__(self):
        self.values = {}
        self.types = {}

    def CreateKey(self, *_args):
        return _RegistryKey(self)

    def OpenKey(self, *_args):
        return _RegistryKey(self)

    def SetValueEx(self, _key, name, _reserved, kind, value):
        self.values[name] = value
        self.types[name] = kind

    def QueryValueEx(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.types.get(name, self.REG_SZ)

    def DeleteValue(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]
        self.types.pop(name, None)


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

    def test_provider_onboarding_validates_and_persists_key_in_secret_store(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            secret_store = MemorySecretStore()
            repositories = ApplicationRepositories(
                config=LocalConfigRepository(
                    root / "config.json",
                    secret_store=secret_store,
                ),
                usage_stats=LocalUsageStatsRepository(root / "usage.json"),
            )
            controller = QmlSettingsController(repositories)
            try:
                self.assertTrue(controller.selectProvider("openai"))
                self.assertFalse(controller.providerHasApiKey)
                controller.setProviderApiKey("onboarding-test-key")

                with patch.object(
                    qml_settings.PROVIDER_REGISTRY,
                    "discover_models",
                    return_value=ModelCatalog(
                        audio_models=("whisper-1",),
                        text_models=("gpt-test",),
                    ),
                ):
                    self.assertTrue(controller.validateProvider())
                    for _ in range(100):
                        self.qt_app.processEvents()
                        if controller.providerStatus == "active":
                            break
                        time.sleep(0.01)

                self.assertEqual(controller.providerStatus, "active")
                self.assertTrue(controller.providerHasApiKey)
                self.assertEqual(secret_store.get("openai"), "onboarding-test-key")
                self.assertEqual(controller.providerApiKey, "")
                self.assertNotIn(
                    "onboarding-test-key",
                    (root / "config.json").read_text(encoding="utf-8"),
                )
            finally:
                controller.shutdown()

    def test_provider_selection_loads_persisted_custom_endpoint(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            secret_store = MemorySecretStore()
            repositories = ApplicationRepositories(
                config=LocalConfigRepository(
                    root / "config.json",
                    secret_store=secret_store,
                ),
                usage_stats=LocalUsageStatsRepository(root / "usage.json"),
            )
            repositories.config.save(
                AppConfig.from_mapping(
                    {
                        "openai_api_key": "saved-key",
                        "openai_base_url": "https://proxy.example/v1",
                    }
                )
            )
            controller = QmlSettingsController(repositories)
            try:
                self.assertTrue(controller.selectProvider("openai"))
                self.assertEqual(
                    controller.providerBaseUrl,
                    "https://proxy.example/v1",
                )
            finally:
                controller.shutdown()

    def test_provider_validation_keeps_unrelated_qml_draft_out_of_persistence(self):
        with TemporaryDirectory() as directory:
            repositories = _repositories(directory)
            controller = QmlSettingsController(repositories)
            try:
                controller.setAutostart(True)
                self.assertTrue(controller.selectWorkflow("rewrite"))
                controller.setRoutePrompt("draft-only prompt")
                self.assertTrue(controller.selectProvider("openai"))
                controller.setProviderApiKey("draft-provider-key")

                with patch.object(
                    qml_settings.PROVIDER_REGISTRY,
                    "discover_models",
                    return_value=ModelCatalog(
                        audio_models=("whisper-1",),
                        text_models=("gpt-test",),
                    ),
                ):
                    self.assertTrue(controller.validateProvider())
                    for _ in range(100):
                        self.qt_app.processEvents()
                        if controller.providerStatus == "active":
                            break
                        time.sleep(0.01)

                persisted = repositories.config.load()
                self.assertTrue(controller.autostart)
                self.assertEqual(controller.routePrompt, "draft-only prompt")
                self.assertFalse(persisted.startup.autostart)
                self.assertNotEqual(
                    persisted.workflow("rewrite").prompt,
                    "draft-only prompt",
                )
                self.assertEqual(persisted.openai.api_key, "draft-provider-key")
            finally:
                controller.shutdown()

    def test_clearing_provider_invalidates_pending_validation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            secret_store = MemorySecretStore()
            repositories = ApplicationRepositories(
                config=LocalConfigRepository(
                    root / "config.json",
                    secret_store=secret_store,
                ),
                usage_stats=LocalUsageStatsRepository(root / "usage.json"),
            )
            repositories.config.save(
                AppConfig.from_mapping({"openai_api_key": "saved-key"})
            )
            controller = QmlSettingsController(repositories)
            started = threading.Event()
            release = threading.Event()

            def blocked_discovery(*_args):
                started.set()
                release.wait(timeout=2)
                return ModelCatalog(audio_models=("whisper-1",))

            try:
                controller.selectProvider("openai")
                with patch.object(
                    qml_settings.PROVIDER_REGISTRY,
                    "discover_models",
                    side_effect=blocked_discovery,
                ):
                    self.assertTrue(controller.validateProvider())
                    self.assertTrue(started.wait(timeout=2))
                    self.assertTrue(controller.clearProvider())
                    release.set()
                    for _ in range(100):
                        self.qt_app.processEvents()
                        time.sleep(0.01)

                self.assertFalse(controller.providerHasApiKey)
                self.assertIsNone(secret_store.get("openai"))
                self.assertEqual(repositories.config.load().openai.api_key, "")
            finally:
                release.set()
                controller.shutdown()

    def test_clear_provider_failure_restores_idle_error_state(self):
        with TemporaryDirectory() as directory:
            controller = QmlSettingsController(_repositories(directory))
            pending = qml_settings.CancellationToken()
            try:
                controller.selectProvider("openai")
                controller._provider_generations["openai"] = 4
                controller._provider_tokens["openai"] = pending
                controller._provider_activity["openai"] = {
                    "status": "validating",
                    "error": "",
                    "models": [],
                    "textModels": [],
                    "busy": True,
                }

                with patch.object(
                    controller,
                    "_persist_provider_credentials",
                    side_effect=OSError("credential store unavailable"),
                ):
                    self.assertFalse(controller.clearProvider())

                self.assertTrue(pending.cancelled)
                self.assertFalse(controller.providerBusy)
                self.assertEqual(controller.providerStatus, "error")
                self.assertEqual(
                    controller.providerError,
                    "credential store unavailable",
                )
                self.assertIn("credential store unavailable", controller.lastError)
            finally:
                controller.shutdown()

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

    def test_source_autostart_command_uses_absolute_qml_entrypoint(self):
        executable = r"C:\Program Files\Python311\python.exe"
        entrypoint = (SETTINGS.parent / "qml_app.py").resolve()

        with patch.object(qml_settings.sys, "frozen", False, create=True):
            command = qml_settings._autostart_command(executable)

        self.assertTrue(entrypoint.is_absolute())
        self.assertEqual(
            command,
            subprocess.list2cmdline([executable, str(entrypoint), "--hidden"]),
        )

    def test_frozen_autostart_command_does_not_append_source_script(self):
        executable = r"C:\Program Files\ClarifyVoice\ClarifyVoice.exe"

        with patch.object(qml_settings.sys, "frozen", True, create=True):
            command = qml_settings._autostart_command(executable)

        self.assertEqual(command, subprocess.list2cmdline([executable, "--hidden"]))

    def test_windows_save_applies_autostart_run_key_and_config(self):
        with TemporaryDirectory() as directory:
            repositories = _repositories(directory)
            registry = _Registry()
            controller = QmlSettingsController(repositories, registry=registry)
            controller.setAutostart(True)
            with (
                patch("spikes.pyside6.qml_settings._is_windows", return_value=True),
                patch.object(
                    repositories.config,
                    "apply",
                    wraps=repositories.config.apply,
                ) as apply,
            ):
                self.assertTrue(controller.save())

            apply.assert_called_once()
            self.assertIn("qml_app.py", registry.values["ClarifyVoice"])
            self.assertIn("--hidden", registry.values["ClarifyVoice"])
            self.assertTrue(repositories.config.load().startup.autostart)
            self.assertFalse(controller.dirty)

    def test_persist_preference_does_not_apply_the_settings_draft(self):
        with TemporaryDirectory() as directory:
            repositories = _repositories(directory)
            repositories.config.save(
                AppConfig.from_mapping(
                    {
                        "ui_mode": "prompt",
                        "ui_language": "en",
                        "autostart": False,
                        "history_enabled": False,
                        "workflows": {
                            "rewrite": {
                                "provider_id": "openai",
                                "model_id": "gpt-4o-mini",
                                "prompt": "Persisted prompt",
                                "enabled": True,
                            }
                        },
                    }
                )
            )
            controller = QmlSettingsController(repositories)
            controller.selectWorkflow("rewrite")
            controller.setRoutePrompt("Draft prompt")
            controller.setHistoryEnabled(True)
            controller.setAutostart(True)

            self.assertTrue(controller.persistMode("transcription"))

            persisted = repositories.config.load()
            self.assertEqual(persisted.ui.mode, "transcription")
            self.assertEqual(persisted.ui.language, "en")
            self.assertFalse(persisted.startup.autostart)
            self.assertFalse(persisted.history_enabled)
            self.assertEqual(
                persisted.workflow("rewrite").prompt,
                "Persisted prompt",
            )
            self.assertEqual(controller.mode, "prompt")
            self.assertEqual(controller.routePrompt, "Draft prompt")
            self.assertTrue(controller.autostart)
            self.assertTrue(controller.historyEnabled)
            self.assertTrue(controller.dirty)

    def test_windows_save_removes_autostart_when_disabled(self):
        with TemporaryDirectory() as directory:
            repositories = _repositories(directory)
            registry = _Registry()
            registry.values["ClarifyVoice"] = r"C:\Legacy\ClarifyVoice.exe --old"
            controller = QmlSettingsController(repositories, registry=registry)

            with patch("spikes.pyside6.qml_settings._is_windows", return_value=True):
                self.assertTrue(controller.save())

            self.assertNotIn("ClarifyVoice", registry.values)
            self.assertNotIn("ClarifyVoice", registry.types)
            self.assertFalse(repositories.config.load().startup.autostart)

    def test_windows_save_restores_registry_when_apply_fails(self):
        with TemporaryDirectory() as directory:
            repositories = _repositories(directory)
            registry = _Registry()
            registry.values["ClarifyVoice"] = r"C:\Legacy\ClarifyVoice.exe --old"
            registry.types["ClarifyVoice"] = 42
            controller = QmlSettingsController(repositories, registry=registry)
            controller.setAutostart(True)
            with (
                patch("spikes.pyside6.qml_settings._is_windows", return_value=True),
                patch.object(
                    repositories.config,
                    "apply",
                    side_effect=OSError("simulated config write failure"),
                ),
            ):
                self.assertFalse(controller.save())

            self.assertEqual(
                registry.values["ClarifyVoice"],
                r"C:\Legacy\ClarifyVoice.exe --old",
            )
            self.assertEqual(registry.types["ClarifyVoice"], 42)
            self.assertFalse(repositories.config.load().startup.autostart)
            self.assertTrue(controller.dirty)
            self.assertIn("simulated config write failure", controller.lastError)

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
