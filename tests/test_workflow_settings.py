"""Headless contracts for the user-facing workflow settings controller (#51)."""

from __future__ import annotations

from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from provider_types import RewriteResult
from repositories import LocalConfigRepository, WorkflowScope
from secret_store import MemorySecretStore
from workflow_settings import WorkflowSettingsController


class WorkflowSettingsControllerTests(unittest.TestCase):
    def repository(self) -> LocalConfigRepository:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return LocalConfigRepository(
            Path(directory.name) / "config.json",
            secret_store=MemorySecretStore(),
        )

    def test_routes_are_drafted_independently_and_apply_transactionally(self):
        controller = WorkflowSettingsController(self.repository())
        original_translation = controller.route(WorkflowScope.TRANSLATION)

        controller.set_route(
            WorkflowScope.REWRITE,
            provider_id="groq",
            model_id="llama-3.3-70b-versatile",
            prompt="Rewrite only the selected text.",
            custom_endpoint="https://proxy.example/v1",
        )
        controller.apply()

        self.assertEqual(controller.route(WorkflowScope.REWRITE).provider_id, "groq")
        self.assertEqual(
            controller.route(WorkflowScope.TRANSLATION), original_translation
        )
        restarted = WorkflowSettingsController(controller.repository)
        self.assertEqual(restarted.route(WorkflowScope.REWRITE).provider_id, "groq")
        self.assertEqual(
            restarted.route(WorkflowScope.TRANSLATION), original_translation
        )

    def test_effective_route_is_safe_and_marks_local_cloud_or_disabled(self):
        controller = WorkflowSettingsController(self.repository())
        controller.set_route(
            WorkflowScope.LOCAL_ASR_REFINEMENT,
            provider_id="openai",
            model_id="gpt-4o-mini",
            enabled=True,
        )
        effective = controller.effective_route(WorkflowScope.LOCAL_ASR_REFINEMENT)

        self.assertEqual(effective["execution"], "cloud")
        self.assertEqual(effective["provider_id"], "openai")
        self.assertNotIn("prompt", effective)
        controller.set_route(
            WorkflowScope.TRANSCRIPTION,
            provider_id="local_asr",
            model_id="ggml-small",
        )
        self.assertEqual(
            controller.effective_route(WorkflowScope.TRANSCRIPTION)["execution"],
            "local",
        )
        controller.set_route(WorkflowScope.LOCAL_ASR_REFINEMENT, enabled=False)
        self.assertEqual(
            controller.effective_route(WorkflowScope.LOCAL_ASR_REFINEMENT)[
                "execution"
            ],
            "disabled",
        )

    def test_test_validates_the_current_draft_before_apply(self):
        controller = WorkflowSettingsController(self.repository())
        persisted_translation = controller.route(WorkflowScope.TRANSLATION)
        controller.set_route(
            WorkflowScope.TRANSLATION,
            provider_id="local_asr",
            model_id="ggml-small",
        )
        with self.assertRaises(ValueError):
            controller.test(WorkflowScope.TRANSLATION)

        # The failed test must not mutate the persisted route or replace the
        # draft with the repository snapshot.
        self.assertEqual(
            controller.repository.load().workflow(WorkflowScope.TRANSLATION),
            persisted_translation,
        )
        self.assertEqual(
            controller.route(WorkflowScope.TRANSLATION).provider_id,
            "local_asr",
        )

    def test_reset_replaces_only_the_selected_draft_scope(self):
        controller = WorkflowSettingsController(self.repository())
        controller.set_route(
            WorkflowScope.REWRITE,
            provider_id="groq",
            model_id="llama-3.3-70b-versatile",
            prompt="unsaved rewrite policy",
        )
        controller.set_route(
            WorkflowScope.TRANSLATION,
            provider_id="groq",
            model_id="llama-3.3-70b-versatile",
            prompt="custom translation policy",
        )
        draft_rewrite = controller.route(WorkflowScope.REWRITE)
        persisted_rewrite = controller.repository.load().workflow(
            WorkflowScope.REWRITE
        )

        reset = controller.reset(WorkflowScope.TRANSLATION)
        self.assertEqual(
            reset.workflow(WorkflowScope.TRANSLATION).provider_id, "openai"
        )
        self.assertNotEqual(
            reset.workflow(WorkflowScope.TRANSLATION).prompt,
            "custom translation policy",
        )
        self.assertEqual(controller.route(WorkflowScope.REWRITE), draft_rewrite)
        self.assertEqual(
            controller.repository.load().workflow(WorkflowScope.REWRITE),
            persisted_rewrite,
        )

    def test_immediate_local_refinement_save_survives_later_apply(self):
        controller = WorkflowSettingsController(self.repository())
        controller.set_route(
            WorkflowScope.REWRITE,
            prompt="unsaved rewrite policy",
        )
        saved_settings = {
            "workflows": controller.repository.load().workflows,
        }

        app._sync_local_asr_refinement_draft(
            controller, saved_settings, True)
        controller.apply()

        restarted = WorkflowSettingsController(controller.repository)
        self.assertTrue(restarted.config.local_asr_cloud_refinement)
        self.assertTrue(
            restarted.route(WorkflowScope.LOCAL_ASR_REFINEMENT).enabled)
        self.assertEqual(
            restarted.route(WorkflowScope.REWRITE).prompt,
            "unsaved rewrite policy",
        )
        self.assertTrue(
            saved_settings["workflows"].route(
                WorkflowScope.LOCAL_ASR_REFINEMENT).enabled)

    def test_immediate_local_refinement_save_updates_selected_widget_both_ways(self):
        class FakeSwitch:
            def __init__(self):
                self.calls = []

            def select(self):
                self.calls.append("select")

            def deselect(self):
                self.calls.append("deselect")

        for enabled, expected_call in ((True, "select"), (False, "deselect")):
            with self.subTest(enabled=enabled):
                switch = FakeSwitch()
                app._sync_selected_workflow_enabled_widget(
                    WorkflowScope.LOCAL_ASR_REFINEMENT,
                    switch,
                    enabled,
                )
                self.assertEqual(switch.calls, [expected_call])

        unrelated = FakeSwitch()
        app._sync_selected_workflow_enabled_widget(
            WorkflowScope.REWRITE, unrelated, True)
        self.assertEqual(unrelated.calls, [])

    def test_reset_baseline_updates_one_scope_and_keeps_other_drafts_dirty(self):
        controller = WorkflowSettingsController(self.repository())
        baseline = controller.repository.load()
        controller.set_route(
            WorkflowScope.REWRITE,
            prompt="unsaved rewrite policy",
        )
        saved_settings = {
            "transcription": ("gemini", "gemini-2.5-flash"),
            "refinement": ("openai", "gpt-4o-mini"),
            "workflows": baseline.workflows,
            "autostart": False,
        }

        reset_config = controller.reset(WorkflowScope.TRANSLATION)
        selected = {"provider": "groq", "model": "whisper-large-v3"}
        selected_refinement = {
            "provider": "groq", "model": "llama-3.3-70b-versatile"
        }
        app._sync_saved_settings_after_workflow_reset(
            saved_settings,
            WorkflowScope.TRANSLATION,
            reset_config.workflow(WorkflowScope.TRANSLATION),
            selected,
            selected_refinement,
        )

        self.assertEqual(
            saved_settings["workflows"].route(WorkflowScope.TRANSLATION),
            reset_config.workflow(WorkflowScope.TRANSLATION),
        )
        self.assertEqual(
            saved_settings["workflows"].route(WorkflowScope.REWRITE),
            baseline.workflow(WorkflowScope.REWRITE),
        )
        self.assertEqual(
            saved_settings["transcription"],
            ("gemini", "gemini-2.5-flash"),
        )
        self.assertEqual(
            saved_settings["refinement"],
            ("openai", "gpt-4o-mini"),
        )
        self.assertNotEqual(
            controller.route(WorkflowScope.REWRITE),
            saved_settings["workflows"].route(WorkflowScope.REWRITE),
        )

    def test_reset_baseline_updates_legacy_compatibility_for_reset_scope(self):
        controller = WorkflowSettingsController(self.repository())
        baseline = controller.repository.load()
        saved_settings = {
            "transcription": ("gemini", "gemini-2.5-flash"),
            "refinement": ("openai", "gpt-4o-mini"),
            "workflows": baseline.workflows,
            "autostart": False,
        }
        selected = {"provider": "groq", "model": "whisper-large-v3"}
        selected_refinement = {
            "provider": "groq", "model": "llama-3.3-70b-versatile"
        }

        app._sync_saved_settings_after_workflow_reset(
            saved_settings,
            WorkflowScope.TRANSCRIPTION,
            baseline.workflow(WorkflowScope.TRANSCRIPTION),
            selected,
            selected_refinement,
        )

        self.assertEqual(
            saved_settings["transcription"],
            ("groq", "whisper-large-v3"),
        )
        self.assertEqual(
            saved_settings["refinement"],
            ("openai", "gpt-4o-mini"),
        )

    def test_forced_cloud_selection_updates_transcription_draft(self):
        controller = WorkflowSettingsController(self.repository())
        controller.set_route(
            WorkflowScope.TRANSCRIPTION,
            provider_id="local_asr",
            model_id="ggml-small",
        )
        controller.set_route(
            WorkflowScope.REWRITE,
            prompt="unsaved rewrite policy",
        )
        persisted = controller.repository.load()
        forced_route = replace(
            persisted.workflow(WorkflowScope.TRANSCRIPTION),
            provider_id="openai",
            model_id="whisper-1",
        )
        forced_config = replace(
            persisted,
            workflows=persisted.workflows.with_route(
                WorkflowScope.TRANSCRIPTION, forced_route),
        )
        saved_settings = {"workflows": persisted.workflows}
        with patch.object(app, "_typed_app_config", return_value=forced_config):
            app._sync_forced_cloud_transcription_draft(
                controller,
                saved_settings,
                {"provider": "openai", "model": "whisper-1"},
            )
        controller.apply()

        restarted = WorkflowSettingsController(controller.repository)
        self.assertEqual(
            restarted.route(WorkflowScope.TRANSCRIPTION).provider_id,
            "openai",
        )
        self.assertEqual(
            restarted.route(WorkflowScope.REWRITE).prompt,
            "unsaved rewrite policy",
        )
        self.assertEqual(
            saved_settings["workflows"].route(
                WorkflowScope.TRANSCRIPTION).provider_id,
            "openai",
        )
        self.assertEqual(
            saved_settings["transcription"],
            ("openai", "whisper-1"),
        )


class WorkflowOperationRoutingTests(unittest.TestCase):
    def setUp(self):
        self.original = app.APP_CONFIG.copy()
        self.addCleanup(self._restore_config)

    def _restore_config(self):
        app.APP_CONFIG.clear()
        app.APP_CONFIG.update(self.original)

    def test_rewrite_operation_uses_its_independent_route(self):
        app.APP_CONFIG["workflows"] = {
            "refinement": {
                "provider_id": "openai", "model_id": "gpt-4o-mini",
                "prompt": "refine", "custom_endpoint": "", "enabled": True,
            },
            "rewrite": {
                "provider_id": "groq", "model_id": "llama-3.3-70b-versatile",
                "prompt": "Rewrite with the selected policy.",
                "custom_endpoint": "https://proxy.example/v1", "enabled": True,
            },
        }
        captured = {}

        def rewrite(_provider, request, _connection, _cancel=None):
            captured["request"] = request
            return RewriteResult("rewritten", "groq", request.model)

        with patch.object(app.PROVIDER_REGISTRY, "rewrite", side_effect=rewrite), \
                patch.object(app, "_provider_connection") as connection:
            result = app.AppWorkflowProvider.rewrite("source")

        self.assertEqual(result.text, "rewritten")
        self.assertEqual(captured["request"].model, "llama-3.3-70b-versatile")
        self.assertIn("selected policy", captured["request"].instruction)
        connection.assert_called_once()
        self.assertEqual(connection.call_args.args[1].custom_endpoint,
                         "https://proxy.example/v1")

    def test_effective_route_summaries_omit_prompts_and_mark_execution(self):
        app.APP_CONFIG["workflows"] = {
            "transcription": {
                "provider_id": "local_asr", "model_id": "ggml-small",
                "prompt": "private", "custom_endpoint": "", "enabled": True,
            },
            "translation": {
                "provider_id": "openai", "model_id": "gpt-4o-mini",
                "prompt": "private", "custom_endpoint": "https://proxy.example/v1",
                "enabled": True,
            },
        }
        summary = app.AppWorkflowConfig.effective_routes()
        self.assertEqual(summary["transcription"]["execution"], "local")
        self.assertEqual(summary["translation"]["execution"], "cloud")
        self.assertEqual(summary["translation"]["custom_endpoint"],
                         "https://proxy.example/v1")
        self.assertNotIn("prompt", summary["translation"])


if __name__ == "__main__":
    unittest.main()
