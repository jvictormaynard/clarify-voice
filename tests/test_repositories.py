import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repositories import (
    CONFIG_SCHEMA_VERSION,
    AppConfig,
    LocalConfigRepository,
    LocalUsageStatsRepository,
    UnsupportedSchemaVersionError,
    migrate_config_payload,
)


class ConfigurationRepositoryTests(unittest.TestCase):
    def test_legacy_flat_config_loads_and_unknown_fields_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "transcription_provider": "openai",
                "openai_api_key": "secret",
                "openai_audio_model": "whisper 1",
                "unknown_future_setting": {"unsafe": True},
            }), encoding="utf-8")

            config = LocalConfigRepository(path).load()

        self.assertEqual(config.selection.transcription_provider, "openai")
        self.assertEqual(config.openai.api_key, "secret")
        self.assertEqual(config.openai.audio_model, "whisper-1")
        self.assertFalse(hasattr(config, "unknown_future_setting"))

    def test_invalid_values_fall_back_without_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "transcription_provider": {"not": "a string"},
                "ui_language": 42,
                "autostart": "yes",
            }), encoding="utf-8")
            config = LocalConfigRepository(path).load()

        self.assertEqual(config.selection.transcription_provider, "gemini")
        self.assertEqual(config.ui.language, "en")
        self.assertFalse(config.startup.autostart)

    def test_save_round_trip_writes_explicit_schema_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            repository = LocalConfigRepository(path)
            repository.save(AppConfig.from_mapping({
                "ui_mode": "transcription",
                "ui_language": "pt",
            }))
            payload = json.loads(path.read_text(encoding="utf-8"))
            loaded = repository.load()

        self.assertEqual(payload["schema_version"], CONFIG_SCHEMA_VERSION)
        self.assertEqual(loaded.ui.mode, "transcription")
        self.assertEqual(loaded.ui.language, "pt")
        self.assertNotIn("unknown_future_setting", payload)

    def test_legacy_adapter_preserves_autostart_on_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = LocalConfigRepository(Path(directory) / "config.json")
            repository.save({"autostart": True})
            config = repository.load()
            legacy = config.to_legacy_mapping()
            repository.save(legacy)

            self.assertTrue(legacy["autostart"])
            self.assertTrue(repository.load().startup.autostart)

    def test_injected_repository_becomes_the_app_compatibility_source(self):
        import app
        from repositories import ApplicationRepositories

        with tempfile.TemporaryDirectory() as directory:
            default_path = Path(directory) / "default.json"
            injected_path = Path(directory) / "injected.json"
            default = LocalConfigRepository(default_path)
            injected = LocalConfigRepository(injected_path)
            default.save({"transcription_provider": "gemini", "ui_mode": "prompt"})
            injected.save({
                "transcription_provider": "openai",
                "openai_api_key": "injected-key",
                "ui_mode": "transcription",
            })
            bundle = ApplicationRepositories(
                config=injected,
                usage_stats=LocalUsageStatsRepository(Path(directory) / "stats.json"),
            )
            default_bundle = ApplicationRepositories(
                config=default,
                usage_stats=LocalUsageStatsRepository(Path(directory) / "default-stats.json"),
            )
            original = app.APP_CONFIG.copy()
            try:
                with patch.object(app, "APP_REPOSITORIES", default_bundle):
                    app._activate_repositories(bundle)
                    self.assertEqual(app.APP_CONFIG["transcription_provider"], "openai")
                    self.assertEqual(app.APP_CONFIG["openai_api_key"], "injected-key")
                    self.assertEqual(app.APP_CONFIG["ui_mode"], "transcription")
                    app._save_app_config(bundle)
                    self.assertEqual(
                        injected.load().selection.transcription_provider, "openai")

                    # This is the lifecycle used by a later App() with no
                    # explicit bundle: the default repository must be loaded
                    # again instead of retaining injected compatibility state.
                    app._activate_repositories(None)
                    self.assertEqual(app.APP_CONFIG["transcription_provider"], "gemini")
                    self.assertEqual(app.APP_CONFIG["ui_mode"], "prompt")
                    self.assertEqual(app.APP_CONFIG["openai_api_key"], "")
                    self.assertEqual(
                        default.load().selection.transcription_provider, "gemini")
            finally:
                app.APP_CONFIG.clear()
                app.APP_CONFIG.update(original)

    def test_autostart_apply_updates_registry_and_repository_together(self):
        import app
        from repositories import ApplicationRepositories

        class Key:
            def __init__(self, registry):
                self.registry = registry

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Registry:
            HKEY_CURRENT_USER = 1
            REG_SZ = 1

            def __init__(self):
                self.values = {}
                self.types = {}

            def CreateKey(self, *_args):
                return Key(self)

            def OpenKey(self, *_args):
                return Key(self)

            def SetValueEx(self, _key, name, _reserved, _kind, value):
                self.values[name] = value
                self.types[name] = _kind

            def QueryValueEx(self, _key, name):
                if name not in self.values:
                    raise FileNotFoundError(name)
                return self.values[name], self.types.get(name, self.REG_SZ)

            def DeleteValue(self, _key, name):
                if name not in self.values:
                    raise FileNotFoundError(name)
                del self.values[name]

        class FailingConfigRepository(LocalConfigRepository):
            def save(self, _config):
                raise OSError("simulated config write failure")

        with tempfile.TemporaryDirectory() as directory:
            config_repository = LocalConfigRepository(Path(directory) / "config.json")
            config_repository.save({"autostart": True})
            bundle = ApplicationRepositories(
                config=config_repository,
                usage_stats=LocalUsageStatsRepository(Path(directory) / "stats.json"),
            )
            registry = Registry()
            original = app.APP_CONFIG.copy()
            try:
                app._activate_repositories(bundle)
                failing_bundle = ApplicationRepositories(
                    config=FailingConfigRepository(config_repository.path),
                    usage_stats=bundle.usage_stats,
                )
                # The persisted value is true while the Registry entry is
                # absent. A failed Apply must preserve that divergence.
                with patch.object(app, "IS_WIN", True):
                    with self.assertRaises(OSError):
                        app._persist_autostart_preference(False, failing_bundle, registry)
                self.assertTrue(app.APP_CONFIG["autostart"])
                self.assertNotIn("ClarifyVoice", registry.values)
                self.assertTrue(config_repository.load().startup.autostart)

                with patch.object(app, "IS_WIN", True):
                    app._persist_autostart_preference(False, bundle, registry)
                self.assertFalse(app.APP_CONFIG["autostart"])
                self.assertNotIn("ClarifyVoice", registry.values)
                self.assertFalse(config_repository.load().startup.autostart)

                with patch.object(app, "IS_WIN", True):
                    app._persist_autostart_preference(True, bundle, registry)
                self.assertTrue(app.APP_CONFIG["autostart"])
                self.assertIn("ClarifyVoice", registry.values)
                self.assertTrue(config_repository.load().startup.autostart)

                custom_command = r"C:\Legacy\ClarifyVoice.exe --custom-start"
                registry.values["ClarifyVoice"] = custom_command
                registry.types["ClarifyVoice"] = 42
                with patch.object(app, "IS_WIN", True):
                    with self.assertRaises(OSError):
                        app._persist_autostart_preference(False, failing_bundle, registry)
                self.assertEqual(registry.values["ClarifyVoice"], custom_command)
                self.assertEqual(registry.types["ClarifyVoice"], 42)
            finally:
                app.APP_CONFIG.clear()
                app.APP_CONFIG.update(original)

    def test_settings_transaction_rolls_back_model_changes_on_save_failure(self):
        import app
        from repositories import ApplicationRepositories

        class FailingConfigRepository(LocalConfigRepository):
            def save(self, _config):
                raise OSError("simulated config write failure")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            seed = LocalConfigRepository(path)
            seed.save({
                "transcription_provider": "gemini",
                "gemini_model": "gemini-2.5-flash",
                "refinement_provider": "openai",
                "refinement_model": "gpt-4o-mini",
                "autostart": False,
            })
            repository = FailingConfigRepository(path)
            bundle = ApplicationRepositories(
                config=repository,
                usage_stats=LocalUsageStatsRepository(Path(directory) / "stats.json"),
            )
            original = app.APP_CONFIG.copy()
            try:
                app._activate_repositories(bundle)
                before = app.APP_CONFIG.copy()
                with self.assertRaises(OSError):
                    app._apply_settings_transaction(
                        {"provider": "openai", "model": "whisper-1"},
                        {"provider": "groq", "model": "llama-3.3-70b-versatile"},
                        [("openai", "whisper-1")],
                        [("groq", "llama-3.3-70b-versatile")],
                        {"openai": "openai_audio_model"},
                        False,
                        bundle,
                    )
                self.assertEqual(app.APP_CONFIG, before)
                self.assertEqual(seed.load().selection.transcription_provider, "gemini")
                self.assertEqual(seed.load().gemini.audio_model, "gemini-2.5-flash")
            finally:
                app.APP_CONFIG.clear()
                app.APP_CONFIG.update(original)


class ConfigurationMigrationTests(unittest.TestCase):
    def test_legacy_migration_is_ordered_and_idempotent(self):
        legacy = {"ui_language": "de"}
        migrated = migrate_config_payload(legacy)

        self.assertEqual(migrated["schema_version"], CONFIG_SCHEMA_VERSION)
        self.assertEqual(migrate_config_payload(migrated), migrated)

    def test_future_schema_is_read_compatibly(self):
        future = {"schema_version": CONFIG_SCHEMA_VERSION + 10, "ui_language": "es",
                  "unknown": object()}
        migrated = migrate_config_payload(future)

        self.assertEqual(migrated["schema_version"], CONFIG_SCHEMA_VERSION + 10)
        self.assertEqual(migrated["ui_language"], "es")

    def test_future_schema_loads_but_save_refuses_a_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "schema_version": CONFIG_SCHEMA_VERSION + 1,
                "ui_language": "es",
                "future_setting": {"keep": "me"},
            }), encoding="utf-8")
            original = path.read_bytes()
            repository = LocalConfigRepository(path)
            config = repository.load()

            self.assertEqual(config.ui.language, "es")
            with self.assertRaises(UnsupportedSchemaVersionError):
                repository.save(config)

            self.assertEqual(path.read_bytes(), original)

    def test_future_schema_save_is_refused_without_prior_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "schema_version": CONFIG_SCHEMA_VERSION + 1,
                "future_setting": {"keep": "me"},
            }), encoding="utf-8")
            original = path.read_bytes()

            with self.assertRaises(UnsupportedSchemaVersionError):
                LocalConfigRepository(path).save({"ui_language": "pt"})

            self.assertEqual(path.read_bytes(), original)

    def test_future_schema_change_between_load_and_save_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"schema_version": CONFIG_SCHEMA_VERSION}),
                            encoding="utf-8")
            repository = LocalConfigRepository(path)
            config = repository.load()
            path.write_text(json.dumps({
                "schema_version": CONFIG_SCHEMA_VERSION + 1,
                "future_setting": {"keep": "me"},
            }), encoding="utf-8")
            original = path.read_bytes()

            with self.assertRaises(UnsupportedSchemaVersionError):
                repository.save(config)

            self.assertEqual(path.read_bytes(), original)


class UsageStatisticsRepositoryTests(unittest.TestCase):
    def test_invalid_payload_and_unknown_events_are_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage_stats.json"
            path.write_text(json.dumps({"events": [{"type": "recording"}, "bad"]}),
                            encoding="utf-8")
            repository = LocalUsageStatsRepository(path)
            self.assertEqual(repository.load_events(), [{"type": "recording"}])

    def test_append_is_atomic_and_preserves_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage_stats.json"
            repository = LocalUsageStatsRepository(path)
            repository.append({"type": "recording", "models": []})
            repository.append({"type": "rewrite", "models": []})
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual([event["type"] for event in payload["events"]],
                         ["recording", "rewrite"])
        self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_future_schema_append_is_refused_without_rewriting_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage_stats.json"
            path.write_text(json.dumps({
                "schema_version": 2,
                "future_setting": {"keep": "me"},
                "events": [],
            }), encoding="utf-8")
            original = path.read_bytes()

            with self.assertRaises(UnsupportedSchemaVersionError):
                LocalUsageStatsRepository(path).append({"type": "recording"})

            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
