import json
import tempfile
import unittest
from pathlib import Path

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
            original = app.APP_CONFIG.copy()
            try:
                app._activate_repositories(bundle)
                self.assertEqual(app.APP_CONFIG["transcription_provider"], "openai")
                self.assertEqual(app.APP_CONFIG["openai_api_key"], "injected-key")
                self.assertEqual(app.APP_CONFIG["ui_mode"], "transcription")
                app._save_app_config(bundle)
                self.assertEqual(injected.load().selection.transcription_provider, "openai")
                self.assertEqual(default.load().selection.transcription_provider, "gemini")
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


if __name__ == "__main__":
    unittest.main()
