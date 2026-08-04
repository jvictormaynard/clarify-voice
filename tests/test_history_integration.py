"""Focused regressions for opt-in transcription history integration."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from history_integration import HistorySettingsController
from history_store import HistoryStore
from provider_types import TranscriptionResult
from repositories import LocalConfigRepository
from secret_store import MemorySecretStore
from workflows import SelectionTarget, StartDictation, StopDictation, WorkflowService


class DeterministicHistoryClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current


class ImmediateScheduler:
    def call_soon(self, callback):
        callback()

    def run_in_background(self, callback):
        callback()


class WorkflowClock:
    def time(self):
        return 10.0

    def monotonic(self):
        return 10.0


class RuntimeProvider:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def transcribe(self, _audio_source, _mode, _language):
        if self.fail:
            error = RuntimeError("provider failed; api_key=runtime-secret")
            error.raw_text = "partial provider transcript"
            error.provider_id = "fake-provider"
            error.model = "fake-model"
            raise error
        return TranscriptionResult(
            "refined transcript",
            "fake-provider",
            "fake-model",
            raw_text="raw provider transcript",
            refined_text="refined transcript",
        )

    def rewrite(self, _text):
        raise AssertionError("rewrite is outside this integration test")

    def translate(self, _text, _language):
        raise AssertionError("translation is outside this integration test")


class RuntimeAudio:
    def __init__(self) -> None:
        self.completed = 0
        self.failures = []

    def microphone_available(self):
        return True

    def create_session(self):
        return self

    def start(self):
        return None

    def wait_until_started(self):
        return None

    def stop(self):
        return SimpleNamespace(
            audio_path=Path("not-persisted.wav"),
            audio_bytes=b"audio-that-must-not-be-stored",
            cancel_token=None,
        )

    def complete(self):
        self.completed += 1
        return True

    def fail(self, error):
        self.failures.append(error)

    def cancel(self):
        return None


class RuntimeClipboard:
    def __init__(self) -> None:
        self.outputs = []

    def write_dictation_result(self, _target, text):
        self.outputs.append(text)


class RuntimeConfig:
    def recording_usage_context(self, mode):
        return {
            "mode": mode,
            "provider": "fake-provider",
            "model": "fake-model",
        }


class RuntimeStatistics:
    def __init__(self) -> None:
        self.dictations = []

    def record_dictation(self, context, duration_seconds, result):
        self.dictations.append((context, duration_seconds, result))


class HistoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="clarifyvoice-history-")
        self.root = Path(self.directory.name)
        self.config_path = self.root / "config.json"
        self.history_path = self.root / "history.json"
        self.secrets = MemorySecretStore()
        self.clock = DeterministicHistoryClock()

    def tearDown(self):
        self.directory.cleanup()

    def make_controller(self):
        repository = LocalConfigRepository(
            self.config_path,
            environment={},
            secret_store=self.secrets,
        )
        return HistorySettingsController(
            repository,
            self.history_path,
            clock=self.clock,
        )

    def test_default_off_startup_and_runtime_record_create_no_history_file(self):
        controller = self.make_controller()

        self.assertIsNone(controller.startup())
        controller.record_transcription(
            raw_text="private raw",
            refined_text="private refined",
            provider="fake-provider",
            model="fake-model",
            status="success",
            error=None,
        )

        self.assertFalse(self.history_path.exists())
        self.assertFalse(self.config_path.exists())
        self.assertEqual(controller.records(), [])

    def test_enable_persist_and_reload_round_trip(self):
        controller = self.make_controller()

        settings = controller.apply(enabled=True, retention_days=None)
        self.assertTrue(settings.enabled)
        controller.record_transcription(
            raw_text="rough wording",
            refined_text="clear wording",
            provider="fake-provider",
            model="fake-model",
            status="success",
            error=None,
        )
        self.assertTrue(self.history_path.exists())

        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["history"]["schema_version"], 1)
        self.assertTrue(payload["history"]["enabled"])
        self.assertIsNone(payload["history"]["retention_days"])

        restarted = self.make_controller()
        self.assertIsNone(restarted.startup())
        records = restarted.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].raw_text, "rough wording")
        self.assertEqual(records[0].refined_text, "clear wording")
        self.assertEqual(records[0].provider, "fake-provider")
        self.assertEqual(records[0].model, "fake-model")

    def test_retention_delete_and_export_preserve_only_safe_record_fields(self):
        controller = self.make_controller()
        controller.apply(enabled=True, retention_days=None)
        controller.store.add(
            raw_text="old text", provider="p", model="m",
            timestamp=self.clock.current - timedelta(days=10),
        )
        controller.store.add(
            raw_text="recent text", refined_text="recent refined",
            provider="p", model="m",
        )

        controller.apply(retention_days=2)
        records = controller.records()
        self.assertEqual([record.raw_text for record in records], ["recent text"])

        json_export = self.root / "history-export.json"
        markdown_export = self.root / "history-export.md"
        text_export = self.root / "history-export.txt"
        controller.export(json_export, format="json")
        controller.export(markdown_export, format="markdown")
        controller.export(text_export, format="txt")
        exported = json_export.read_text(encoding="utf-8")
        self.assertIn("recent refined", exported)
        self.assertNotIn("audio-that-must-not-be-stored", exported)
        self.assertNotIn("api_key", exported)
        self.assertIn("# ClarifyVoice transcription history", markdown_export.read_text(
            encoding="utf-8"))
        self.assertIn("Raw transcript:", text_export.read_text(encoding="utf-8"))

        controller.delete_all()
        self.assertFalse(self.history_path.exists())
        self.assertEqual(controller.records(), [])
        self.assertTrue(json_export.exists())

    def test_failed_and_partial_records_are_statused_and_redacted(self):
        controller = self.make_controller()
        controller.apply(enabled=True, retention_days=None)
        controller.record_transcription(
            raw_text="partial raw",
            refined_text=None,
            provider="fake-provider",
            model="fake-model",
            status="partial",
            error="HTTP 502; api_key=live-secret; token=live-token",
        )
        controller.record_transcription(
            raw_text=None,
            refined_text=None,
            provider="fake-provider",
            model="fake-model",
            status="error",
            error="provider request failed",
        )

        records = controller.records()
        self.assertEqual([record.status for record in records], ["partial", "error"])
        self.assertEqual(records[0].raw_text, "partial raw")
        self.assertNotIn("live-secret", records[0].error or "")
        self.assertNotIn("live-token", records[0].error or "")
        serialized = self.history_path.read_text(encoding="utf-8")
        self.assertNotIn("live-secret", serialized)
        self.assertNotIn("live-token", serialized)
        self.assertNotIn('"audio"', serialized)
        self.assertNotIn('"payload"', serialized)

    def run_dictation(self, controller, provider):
        audio = RuntimeAudio()
        clipboard = RuntimeClipboard()
        statistics = RuntimeStatistics()
        service = WorkflowService(
            provider,
            audio,
            clipboard,
            RuntimeConfig(),
            statistics,
            ImmediateScheduler(),
            WorkflowClock(),
            history=controller,
        )
        self.assertTrue(service.dispatch(
            StartDictation(SelectionTarget(7, "editor.exe"), "prompt", "en")
        ))
        self.assertTrue(service.dispatch(StopDictation()))
        return service, audio, clipboard, statistics

    def test_runtime_history_is_opt_in_and_never_enters_usage_statistics(self):
        disabled = self.make_controller()
        service, _audio, clipboard, statistics = self.run_dictation(
            disabled, RuntimeProvider()
        )
        self.assertEqual(service.state.phase.value, "completed")
        self.assertFalse(self.history_path.exists())
        self.assertEqual(clipboard.outputs, ["refined transcript"])
        self.assertEqual(statistics.dictations[0][2], "refined transcript")
        self.assertNotIn("raw provider transcript", json.dumps(statistics.dictations))

        enabled = self.make_controller()
        enabled.apply(enabled=True, retention_days=None)
        _service, _audio, _clipboard, enabled_statistics = self.run_dictation(
            enabled, RuntimeProvider()
        )
        records = enabled.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].raw_text, "raw provider transcript")
        self.assertEqual(records[0].refined_text, "refined transcript")
        self.assertEqual(records[0].status, "success")
        self.assertEqual(records[0].provider, "fake-provider")
        self.assertEqual(records[0].model, "fake-model")
        self.assertNotIn("raw provider transcript",
                         json.dumps(enabled_statistics.dictations))
        serialized = self.history_path.read_text(encoding="utf-8")
        self.assertNotIn("audio-that-must-not-be-stored", serialized)

    def test_runtime_provider_failure_records_partial_without_audio(self):
        controller = self.make_controller()
        controller.apply(enabled=True, retention_days=None)

        service, audio, _clipboard, _statistics = self.run_dictation(
            controller, RuntimeProvider(fail=True)
        )

        self.assertEqual(service.state.phase.value, "failed")
        self.assertEqual(len(audio.failures), 1)
        records = controller.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "partial")
        self.assertEqual(records[0].raw_text, "partial provider transcript")
        self.assertIsNone(records[0].refined_text)
        self.assertNotIn("runtime-secret", records[0].error or "")


if __name__ == "__main__":
    unittest.main()
