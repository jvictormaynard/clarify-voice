import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import app
from history_store import HistoryStore
from repositories import AppConfig
from workflows import WorkflowKind, WorkflowPhase, WorkflowState


class HistoryRuntimeTests(unittest.TestCase):
    def test_history_preferences_round_trip_through_typed_config(self):
        config = AppConfig.from_mapping({
            "history_enabled": True,
            "history_retention_days": 90,
        })

        self.assertTrue(config.history_enabled)
        self.assertEqual(config.history_retention_days, 90)
        values = config.to_mapping()
        self.assertTrue(values["history_enabled"])
        self.assertEqual(values["history_retention_days"], 90)

    def test_malformed_history_preferences_are_safe_defaults(self):
        config = AppConfig.from_mapping({
            "history_enabled": "yes",
            "history_retention_days": -1,
        })

        self.assertFalse(config.history_enabled)
        self.assertEqual(config.history_retention_days, 30)

    def test_terminal_text_workflow_is_recorded_without_usage_stats(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "history.json", enabled=True)
            harness = SimpleNamespace(history_store=store)
            app.App._record_history_state(
                harness,
                WorkflowState(
                    phase=WorkflowPhase.COMPLETED,
                    operation_id=3,
                    kind=WorkflowKind.REWRITE,
                    source_text="source text",
                    result_text="rewritten text",
                    provider_id="openai",
                    model="gpt-test",
                ),
            )

            records = store.list_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].raw_text, "source text")
            self.assertEqual(records[0].refined_text, "rewritten text")
            self.assertEqual(records[0].workflow, "rewrite")
            self.assertEqual(records[0].provider, "openai")
            self.assertEqual(records[0].model, "gpt-test")
            self.assertEqual(records[0].status, "success")

    def test_dictation_uses_transcript_as_raw_text(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "history.json", enabled=True)
            harness = SimpleNamespace(history_store=store)
            app.App._record_history_state(
                harness,
                WorkflowState(
                    phase=WorkflowPhase.COMPLETED,
                    operation_id=4,
                    kind=WorkflowKind.DICTATION,
                    result_text="spoken words",
                    provider_id="gemini",
                    model="gemini-test",
                ),
            )

            record = store.list_records()[0]
            self.assertEqual(record.raw_text, "spoken words")
            self.assertIsNone(record.refined_text)

    def test_disabled_history_does_not_create_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            harness = SimpleNamespace(history_store=HistoryStore(path))
            app.App._record_history_state(
                harness,
                WorkflowState(
                    phase=WorkflowPhase.COMPLETED,
                    kind=WorkflowKind.DICTATION,
                    result_text="must not persist",
                ),
            )
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
