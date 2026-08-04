import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
from history_store import HistoryStore, HistoryStoreError
from repositories import (
    AppConfig,
    ApplicationRepositories,
    WorkflowRoute,
    WorkflowScope,
)
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

    def test_prompt_mode_dictation_keeps_refinement_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "history.json", enabled=True)
            harness = SimpleNamespace(history_store=store)
            app.App._record_history_state(
                harness,
                WorkflowState(
                    phase=WorkflowPhase.COMPLETED,
                    operation_id=5,
                    kind=WorkflowKind.DICTATION,
                    source_text="raw transcript",
                    result_text="refined transcript",
                    refined_text="refined transcript",
                    provider_id="gemini",
                    model="gemini-audio",
                    refinement_provider_id="openai",
                    refinement_model="gpt-test",
                ),
            )

            record = store.list_records()[0]
            self.assertEqual(record.raw_text, "raw transcript")
            self.assertEqual(record.refined_text, "refined transcript")
            self.assertEqual(record.provider, "gemini")
            self.assertEqual(record.model, "gemini-audio")
            self.assertEqual(record.refinement_provider, "openai")
            self.assertEqual(record.refinement_model, "gpt-test")

            exported = store.export(
                Path(directory) / "history.txt", format="txt").read_text()
            self.assertIn("Refinement provider: openai", exported)
            self.assertIn("Refinement model: gpt-test", exported)

    def test_audio_details_preserve_raw_and_refined_routes(self):
        transcription_route = WorkflowRoute(
            provider_id="gemini", model_id="gemini-audio", prompt="")
        refinement_route = WorkflowRoute(
            provider_id="openai", model_id="gpt-test", prompt="")
        metadata = SimpleNamespace(
            supports=lambda capability: capability
            != app.ProviderCapability.MULTIMODAL_AUDIO)
        with patch.object(
                app.PROVIDER_REGISTRY, "describe", return_value=metadata), \
                patch.object(
                    app.PROVIDER_REGISTRY, "transcribe",
                    return_value=SimpleNamespace(text="raw transcript")), \
                patch.object(
                    app, "_provider_connection", return_value=object()), \
                patch.object(
                    app, "_workflow_route",
                    side_effect=lambda scope: (
                        refinement_route
                        if scope == WorkflowScope.REFINEMENT
                        else transcription_route)), \
                patch.object(
                    app.DICTIONARY_SERVICE, "apply_context",
                    side_effect=lambda request: request), \
                patch.object(
                    app.DICTIONARY_SERVICE, "expand",
                    side_effect=lambda text: text), \
                patch.object(
                    app, "_refine_transcript", return_value="refined transcript"):
            result = app._call_provider_audio(
                "gemini", Path("audio.wav"), "prompt",
                route=transcription_route, details=True)

        self.assertEqual(result.raw_text, "raw transcript")
        self.assertEqual(result.refined_text, "refined transcript")
        self.assertEqual(result.provider_id, "gemini")
        self.assertEqual(result.model, "gemini-audio")
        self.assertEqual(result.refinement_provider_id, "openai")
        self.assertEqual(result.refinement_model, "gpt-test")

    def test_audio_details_without_refinement_keep_legacy_history_shape(self):
        transcription_route = WorkflowRoute(
            provider_id="gemini", model_id="gemini-audio", prompt="")
        metadata = SimpleNamespace(
            supports=lambda capability: capability
            == app.ProviderCapability.MULTIMODAL_AUDIO)
        with patch.object(
                app.PROVIDER_REGISTRY, "describe", return_value=metadata), \
                patch.object(
                    app.PROVIDER_REGISTRY, "transcribe",
                    return_value=SimpleNamespace(text="final transcript")), \
                patch.object(
                    app, "_provider_connection", return_value=object()), \
                patch.object(
                    app, "_workflow_route", return_value=transcription_route), \
                patch.object(
                    app.DICTIONARY_SERVICE, "apply_context",
                    side_effect=lambda request: request), \
                patch.object(
                    app.DICTIONARY_SERVICE, "expand",
                    side_effect=lambda text: text), \
                patch.object(app, "_refine_transcript") as refine:
            result = app._call_provider_audio(
                "gemini", Path("audio.wav"), "prompt",
                route=transcription_route, details=True)

        self.assertEqual(result.text, "final transcript")
        self.assertIsNone(result.raw_text)
        self.assertIsNone(result.refined_text)
        self.assertIsNone(result.refinement_provider_id)
        self.assertIsNone(result.refinement_model)
        refine.assert_not_called()

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

    def test_pathless_injected_repository_requires_explicit_history_path(self):
        pathless = SimpleNamespace()
        with self.assertRaises(ValueError):
            app._history_path_for_repositories(
                ApplicationRepositories(config=pathless, usage_stats=pathless))

        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory) / "isolated-history.json"
            bundle = ApplicationRepositories(
                config=pathless, usage_stats=pathless, history_path=expected)
            self.assertEqual(app._history_path_for_repositories(bundle), expected)

    def test_refresh_load_reports_corruption_instead_of_success(self):
        class BrokenStore:
            enabled = True

            @staticmethod
            def list_records():
                raise HistoryStoreError("corrupt history")

        loaded, records, error = app._load_history_records(BrokenStore())
        self.assertFalse(loaded)
        self.assertEqual(records, [])
        self.assertIsInstance(error, HistoryStoreError)


if __name__ == "__main__":
    unittest.main()
