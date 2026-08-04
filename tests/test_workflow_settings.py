"""Headless contracts for the user-facing workflow settings controller (#51)."""

from __future__ import annotations

from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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

    def test_local_refinement_toggle_keeps_other_draft_fields_dirty(self):
        controller = WorkflowSettingsController(self.repository())
        persisted = controller.repository.load()
        baseline_route = persisted.workflow(
            WorkflowScope.LOCAL_ASR_REFINEMENT)
        controller.set_route(
            WorkflowScope.LOCAL_ASR_REFINEMENT,
            provider_id="groq",
            model_id="llama-3.3-70b-versatile",
            prompt="draft local refinement policy",
            custom_endpoint="https://proxy.example/v1",
        )
        draft_before_toggle = controller.route(
            WorkflowScope.LOCAL_ASR_REFINEMENT)
        saved_settings = {"workflows": persisted.workflows}

        app._sync_local_asr_refinement_draft(
            controller, saved_settings, True)

        draft_after_toggle = controller.route(
            WorkflowScope.LOCAL_ASR_REFINEMENT)
        saved_route = saved_settings["workflows"].route(
            WorkflowScope.LOCAL_ASR_REFINEMENT)
        self.assertEqual(
            draft_after_toggle,
            replace(draft_before_toggle, enabled=True),
        )
        self.assertEqual(saved_route.provider_id, baseline_route.provider_id)
        self.assertEqual(saved_route.model_id, baseline_route.model_id)
        self.assertEqual(saved_route.prompt, baseline_route.prompt)
        self.assertEqual(
            saved_route.custom_endpoint, baseline_route.custom_endpoint)
        self.assertTrue(saved_route.enabled)
        self.assertNotEqual(draft_after_toggle, saved_route)

    def test_immediate_local_refinement_save_updates_selected_widget_both_ways(self):
        class FakeSwitch:
            def __init__(self):
                self.calls = []

            def select(self):
                self.calls.append("select")

            def deselect(self):
                self.calls.append("deselect")

        controller = WorkflowSettingsController(self.repository())
        route = controller.route(WorkflowScope.LOCAL_ASR_REFINEMENT)
        for enabled, expected_call in ((True, "select"), (False, "deselect")):
            with self.subTest(enabled=enabled):
                switch = FakeSwitch()
                app._sync_selected_workflow_form_widgets(
                    WorkflowScope.LOCAL_ASR_REFINEMENT,
                    WorkflowScope.LOCAL_ASR_REFINEMENT,
                    {"enabled": switch},
                    replace(route, enabled=enabled),
                    fields=("enabled",),
                )
                self.assertEqual(switch.calls, [expected_call])

        unrelated = FakeSwitch()
        app._sync_selected_workflow_form_widgets(
            WorkflowScope.REWRITE,
            WorkflowScope.LOCAL_ASR_REFINEMENT,
            {"enabled": unrelated},
            route,
            fields=("enabled",),
        )
        self.assertEqual(unrelated.calls, [])

    def test_immediate_local_refinement_toggle_only_baselines_enabled(self):
        controller = WorkflowSettingsController(self.repository())
        controller.set_route(
            WorkflowScope.LOCAL_ASR_REFINEMENT,
            provider_id="groq",
            model_id="llama-3.3-70b-versatile",
            prompt="unsaved local refinement policy",
            custom_endpoint="https://local-refinement.example/v1",
        )
        persisted = controller.repository.load().workflows
        saved_settings = {"workflows": persisted}

        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                app._sync_local_asr_refinement_draft(
                    controller, saved_settings, enabled)
                draft = controller.route(WorkflowScope.LOCAL_ASR_REFINEMENT)
                baseline = saved_settings["workflows"].route(
                    WorkflowScope.LOCAL_ASR_REFINEMENT)
                self.assertEqual(draft.provider_id, "groq")
                self.assertEqual(draft.model_id, "llama-3.3-70b-versatile")
                self.assertEqual(draft.prompt, "unsaved local refinement policy")
                self.assertEqual(
                    draft.custom_endpoint,
                    "https://local-refinement.example/v1",
                )
                self.assertEqual(draft.enabled, enabled)
                self.assertEqual(baseline.enabled, enabled)
                self.assertNotEqual(baseline.provider_id, draft.provider_id)
                self.assertNotEqual(baseline.prompt, draft.prompt)

        controller.apply()
        restarted = WorkflowSettingsController(controller.repository)
        applied = restarted.route(WorkflowScope.LOCAL_ASR_REFINEMENT)
        self.assertEqual(applied.provider_id, "groq")
        self.assertEqual(applied.model_id, "llama-3.3-70b-versatile")
        self.assertEqual(applied.prompt, "unsaved local refinement policy")
        self.assertEqual(
            applied.custom_endpoint,
            "https://local-refinement.example/v1",
        )
        self.assertFalse(applied.enabled)

    def test_selected_workflow_form_sync_updates_only_requested_fields(self):
        class FakeMenu:
            def __init__(self, value):
                self.value = value

            def set(self, value):
                self.value = value

        class FakeEntry:
            def __init__(self, value):
                self.value = value

            def delete(self, *_args):
                self.value = ""

            def insert(self, _index, value):
                self.value = value

        controller = WorkflowSettingsController(self.repository())
        route = replace(
            controller.route(WorkflowScope.TRANSCRIPTION),
            provider_id="openai",
            model_id="whisper-1",
        )
        widgets = {
            "provider_menu": FakeMenu("local_asr"),
            "model": FakeEntry("ggml-small"),
            "endpoint": FakeEntry("draft endpoint"),
            "prompt": FakeEntry("draft prompt"),
        }

        app._sync_selected_workflow_form_widgets(
            WorkflowScope.TRANSCRIPTION,
            WorkflowScope.TRANSCRIPTION,
            widgets,
            route,
            fields=("provider_id", "model_id"),
        )

        self.assertEqual(widgets["provider_menu"].value, "openai")
        self.assertEqual(widgets["model"].value, "whisper-1")
        self.assertEqual(widgets["endpoint"].value, "draft endpoint")
        self.assertEqual(widgets["prompt"].value, "draft prompt")

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
            prompt="unsaved transcription policy",
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
        class FakeMenu:
            def __init__(self, value):
                self.value = value

            def set(self, value):
                self.value = value

        class FakeEntry:
            def __init__(self, value):
                self.value = value

            def delete(self, *_args):
                self.value = ""

            def insert(self, _index, value):
                self.value = value

        widgets = {
            "provider_menu": FakeMenu("local_asr"),
            "model": FakeEntry("ggml-small"),
        }
        app._sync_selected_workflow_form_widgets(
            WorkflowScope.TRANSCRIPTION,
            WorkflowScope.TRANSCRIPTION,
            widgets,
            controller.route(WorkflowScope.TRANSCRIPTION),
            fields=("provider_id", "model_id"),
        )
        self.assertEqual(widgets["provider_menu"].value, "openai")
        self.assertEqual(widgets["model"].value, "whisper-1")
        self.assertEqual(
            controller.route(WorkflowScope.TRANSCRIPTION).prompt,
            "unsaved transcription policy",
        )
        controller.apply()

        restarted = WorkflowSettingsController(controller.repository)
        self.assertEqual(
            restarted.route(WorkflowScope.TRANSCRIPTION).provider_id,
            "openai",
        )
        self.assertEqual(
            restarted.route(WorkflowScope.TRANSCRIPTION).prompt,
            "unsaved transcription policy",
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

    def test_local_removal_detects_unapplied_local_transcription_draft(self):
        controller = WorkflowSettingsController(self.repository())
        controller.set_route(
            WorkflowScope.TRANSCRIPTION,
            provider_id="local_asr",
            model_id="ggml-small",
        )

        self.assertTrue(
            app._local_asr_removal_has_transcription_draft_conflict(
                controller, "openai"))
        # An unchanged local persisted route is handled by the removal flow,
        # which first forces and persists the selected cloud route.
        self.assertFalse(
            app._local_asr_removal_has_transcription_draft_conflict(
                controller, "local_asr"))

        controller.set_route(
            WorkflowScope.TRANSCRIPTION,
            provider_id="openai",
            model_id="whisper-1",
        )
        self.assertFalse(
            app._local_asr_removal_has_transcription_draft_conflict(
                controller, "openai"))

    def test_live_workflow_form_values_update_draft_and_dirty_state(self):
        controller = WorkflowSettingsController(self.repository())
        saved_settings = {"workflows": controller.workflows}

        app._store_workflow_form_draft(
            controller,
            WorkflowScope.REWRITE,
            provider_id="groq",
            model_id="llama-3.3-70b-versatile",
            prompt="live rewrite policy",
            custom_endpoint="https://rewrite.example/v1",
            enabled=False,
        )
        current_settings = {"workflows": controller.workflows}
        route = controller.route(WorkflowScope.REWRITE)

        self.assertNotEqual(current_settings, saved_settings)
        self.assertEqual(route.provider_id, "groq")
        self.assertEqual(route.model_id, "llama-3.3-70b-versatile")
        self.assertEqual(route.prompt, "live rewrite policy")
        self.assertEqual(route.custom_endpoint, "https://rewrite.example/v1")
        self.assertFalse(route.enabled)
        self.assertTrue(route.independent)

    def test_primary_scoped_edits_survive_apply_and_stale_flat_runtime(self):
        controller = WorkflowSettingsController(self.repository())
        controller.set_route(
            WorkflowScope.TRANSCRIPTION,
            provider_id="groq",
            model_id="whisper-large-v3",
            prompt="scoped transcription policy",
        )
        controller.set_route(
            WorkflowScope.REFINEMENT,
            provider_id="groq",
            model_id="llama-3.3-70b-versatile",
            prompt="scoped refinement policy",
        )
        controller.apply()
        reloaded = controller.repository.load()
        self.assertTrue(reloaded.workflow(WorkflowScope.TRANSCRIPTION).independent)
        self.assertTrue(reloaded.workflow(WorkflowScope.REFINEMENT).independent)

        original = app.APP_CONFIG.copy()
        self.addCleanup(lambda: (app.APP_CONFIG.clear(),
                                 app.APP_CONFIG.update(original)))
        app.APP_CONFIG.clear()
        app.APP_CONFIG.update(reloaded.to_legacy_mapping())
        # Simulate a stale flat Models picker while the scoped routes remain
        # authored and persisted in the nested mapping.
        app.APP_CONFIG.update({
            "transcription_provider": "gemini",
            "gemini_model": "gemini-2.5-flash",
            "refinement_provider": "openai",
            "refinement_model": "gpt-4o-mini",
        })

        transcription = app._workflow_route(WorkflowScope.TRANSCRIPTION)
        refinement = app._workflow_route(WorkflowScope.REFINEMENT)
        self.assertEqual(transcription.provider_id, "groq")
        self.assertEqual(transcription.model_id, "whisper-large-v3")
        self.assertEqual(refinement.provider_id, "groq")
        self.assertEqual(refinement.model_id, "llama-3.3-70b-versatile")

    def test_scoped_primary_form_edits_record_authorship(self):
        controller = WorkflowSettingsController(self.repository())
        legacy_route = controller.route(WorkflowScope.TRANSCRIPTION)
        unchanged = controller.set_route(
            WorkflowScope.TRANSCRIPTION,
            provider_id=legacy_route.provider_id,
            model_id=legacy_route.model_id,
            prompt=legacy_route.prompt,
            custom_endpoint=legacy_route.custom_endpoint,
            enabled=legacy_route.enabled,
        )
        self.assertFalse(unchanged.independent)

        app._store_workflow_form_draft(
            controller,
            WorkflowScope.TRANSCRIPTION,
            provider_id="groq",
            model_id="whisper-large-v3",
            prompt="scoped transcription policy",
            custom_endpoint="",
            enabled=True,
        )
        app._store_workflow_form_draft(
            controller,
            WorkflowScope.REFINEMENT,
            provider_id="groq",
            model_id="llama-3.3-70b-versatile",
            prompt="scoped refinement policy",
            custom_endpoint="",
            enabled=True,
        )

        self.assertTrue(
            controller.route(WorkflowScope.TRANSCRIPTION).independent)
        self.assertTrue(controller.route(WorkflowScope.REFINEMENT).independent)

    def test_forced_cloud_selection_refreshes_visible_transcription_fields(self):
        controller = WorkflowSettingsController(self.repository())
        controller.set_route(
            WorkflowScope.TRANSCRIPTION,
            provider_id="local_asr",
            model_id="ggml-small",
            prompt="unsaved transcription policy",
            custom_endpoint="https://proxy.example/v1",
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

        class FakeOption:
            def __init__(self):
                self.values = []

            def set(self, value):
                self.values.append(value)

        class FakeEntry:
            def __init__(self, value="local_asr"):
                self.values = [value]

            def delete(self, *_args):
                self.values.clear()

            def insert(self, _index, value):
                self.values.append(value)

        provider_widget = FakeOption()
        model_widget = FakeEntry("ggml-small")
        widgets = {
            "provider_menu": provider_widget,
            "model": model_widget,
        }
        with patch.object(app, "_typed_app_config", return_value=forced_config):
            app._sync_forced_cloud_transcription_draft(
                controller,
                saved_settings,
                {"provider": "openai", "model": "whisper-1"},
            )
            app._sync_selected_workflow_form_widgets(
                WorkflowScope.TRANSCRIPTION,
                WorkflowScope.TRANSCRIPTION,
                widgets,
                controller.route(WorkflowScope.TRANSCRIPTION),
                fields=("provider_id", "model_id"),
            )

        self.assertEqual(provider_widget.values, ["openai"])
        self.assertEqual(model_widget.values, ["whisper-1"])
        route = controller.route(WorkflowScope.TRANSCRIPTION)
        self.assertEqual(route.prompt, "unsaved transcription policy")
        self.assertEqual(route.custom_endpoint, "https://proxy.example/v1")


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

    def test_equal_authored_rewrite_route_ignores_legacy_picker(self):
        app.APP_CONFIG["refinement_provider"] = "groq"
        app.APP_CONFIG["refinement_model"] = "llama-3.3-70b-versatile"
        app.APP_CONFIG["workflows"] = {
            "refinement": {
                "provider_id": "openai", "model_id": "gpt-4o-mini",
                "prompt": "refine", "custom_endpoint": "", "enabled": True,
                "independent": False,
            },
            "rewrite": {
                "provider_id": "openai", "model_id": "gpt-4o-mini",
                "prompt": "authored rewrite policy", "custom_endpoint": "",
                "enabled": True, "independent": True,
            },
        }
        captured = {}

        def rewrite(_provider, request, _connection, _cancel=None):
            captured["request"] = request
            return RewriteResult("rewritten", "openai", request.model)

        with patch.object(app.PROVIDER_REGISTRY, "rewrite", side_effect=rewrite), \
                patch.object(app, "_provider_connection"):
            result = app.AppWorkflowProvider.rewrite("source")

        self.assertEqual(result.text, "rewritten")
        self.assertEqual(result.provider_id, "openai")
        self.assertEqual(captured["request"].model, "gpt-4o-mini")
        self.assertIn("authored rewrite policy", captured["request"].instruction)

    def test_scoped_primary_routes_override_stale_flat_selectors(self):
        original = app.APP_CONFIG.copy()
        self.addCleanup(lambda: (app.APP_CONFIG.clear(),
                                 app.APP_CONFIG.update(original)))
        app.APP_CONFIG.update({
            "transcription_provider": "gemini",
            "refinement_provider": "openai",
            "refinement_model": "gpt-4o-mini",
            "workflows": {
                "transcription": {
                    "provider_id": "groq",
                    "model_id": "whisper-large-v3",
                    "prompt": "scoped transcription",
                    "independent": True,
                },
                "refinement": {
                    "provider_id": "groq",
                    "model_id": "llama-3.3-70b-versatile",
                    "prompt": "scoped refinement",
                    "independent": True,
                },
            },
        })

        transcription = app._workflow_route(WorkflowScope.TRANSCRIPTION)
        refinement = app._workflow_route(WorkflowScope.REFINEMENT)

        self.assertEqual(transcription.provider_id, "groq")
        self.assertEqual(transcription.model_id, "whisper-large-v3")
        self.assertEqual(refinement.provider_id, "groq")
        self.assertEqual(refinement.model_id, "llama-3.3-70b-versatile")

    def test_scoped_transcription_provider_passes_route_to_facade(self):
        app.APP_CONFIG.update({
            "transcription_provider": "gemini",
            "workflows": {
                "transcription": {
                    "provider_id": "groq",
                    "model_id": "whisper-large-v3",
                    "prompt": "scoped transcription",
                    "independent": True,
                },
            },
        })
        source = SimpleNamespace(
            audio_path=Path("recording.wav"),
            audio_bytes=b"audio",
            cancel_token=None,
        )
        with patch.object(
                app, "call_transcription_provider",
                return_value="scoped transcript") as transcribe:
            result = app.AppWorkflowProvider.transcribe(
                source, "transcription", "en")

        self.assertEqual(result.text, "scoped transcript")
        self.assertEqual(result.provider_id, "groq")
        self.assertEqual(result.model, "whisper-large-v3")
        route = transcribe.call_args.kwargs["route"]
        self.assertTrue(route.independent)
        self.assertEqual(route.provider_id, "groq")
        self.assertEqual(route.model_id, "whisper-large-v3")

    def test_scoped_transcription_route_reaches_audio_adapter(self):
        app.APP_CONFIG.update({
            "transcription_provider": "gemini",
            "gemini_api_key": "gemini-key",
            "workflows": {
                "transcription": {
                    "provider_id": "groq",
                    "model_id": "whisper-large-v3",
                    "prompt": "scoped transcription",
                    "independent": True,
                },
            },
        })
        route = app._workflow_route(WorkflowScope.TRANSCRIPTION)
        captured = {}

        def transcribe(provider, request, _connection, _cancel_token):
            captured["provider"] = provider
            captured["model"] = request.model
            return SimpleNamespace(text="scoped transcript")

        with patch.object(app.PROVIDER_REGISTRY, "transcribe", side_effect=transcribe), \
                patch.object(app.DICTIONARY_SERVICE, "apply_context",
                              side_effect=lambda request: request):
            result = app.call_transcription_provider(
                Path("recording.wav"), "transcription", "en", route=route)

        self.assertEqual(result, "scoped transcript")
        self.assertEqual(captured, {
            "provider": "groq",
            "model": "whisper-large-v3",
        })

    def test_audio_import_cannot_bypass_disabled_transcription_route(self):
        harness = SimpleNamespace(_t=lambda key: key)
        disabled_route = app.WorkflowRoute(
            provider_id="local_asr",
            model_id="ggml-small",
            enabled=False,
            independent=True,
        )
        with patch.object(app, "_workflow_route", return_value=disabled_route), \
                patch.object(app, "_provider_connection") as connection:
            with self.assertRaisesRegex(
                    ValueError, "audio_import_route_disabled"):
                app.App._audio_file_import_selection(
                    harness, "openai", "whisper-1", "en", "cloud",)

        connection.assert_not_called()

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
