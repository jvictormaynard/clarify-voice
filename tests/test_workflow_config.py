"""Focused contracts for workflow-scoped provider configuration (#51)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from provider_types import (
    ProviderCapability,
    ProviderConnection,
    UnsupportedCapabilityError,
)
from provider_registry import PROVIDER_REGISTRY
from repositories import (
    CONFIG_SCHEMA_VERSION,
    AppConfig,
    LocalConfigRepository,
    WorkflowConfigurationError,
    WorkflowRoute,
    WorkflowScope,
    migrate_config_payload,
    test_workflow_configuration,
    validate_workflow_config,
)
from secret_store import MemorySecretStore


class WorkflowConfigurationTests(unittest.TestCase):
    def test_migration_splits_legacy_refinement_into_idempotent_scopes(self):
        legacy = {
            "transcription_provider": "groq",
            "groq_audio_model": "Whisper Large V3",
            "refinement_provider": "openai",
            "refinement_model": "gpt-4o-mini",
        }

        migrated = migrate_config_payload(legacy)
        self.assertEqual(migrated["schema_version"], CONFIG_SCHEMA_VERSION)
        self.assertEqual(
            set(migrated["workflows"]),
            {
                "transcription", "refinement", "rewrite", "translation",
                "local_asr_refinement",
            },
        )
        self.assertEqual(
            migrate_config_payload(migrated), migrated,
        )

        config = AppConfig.from_mapping(migrated)
        self.assertEqual(config.workflow("transcription").provider_id, "groq")
        self.assertEqual(
            config.workflow("transcription").model_id, "whisper-large-v3"
        )
        self.assertEqual(
            config.workflow("rewrite").provider_id,
            config.workflow("translation").provider_id,
        )

    def test_routes_are_independent_and_custom_endpoint_is_not_a_secret(self):
        config = AppConfig.from_mapping({
            "transcription_provider": "gemini",
            "workflows": {
                "transcription": {
                    "provider_id": "gemini",
                    "model_id": "gemini-2.5-flash",
                    "prompt": "only transcribe",
                },
                "rewrite": {
                    "provider_id": "openai",
                    "model_id": "gpt-4o-mini",
                    "prompt": "rewrite only",
                    "custom_endpoint": "https://proxy.example/v1",
                },
                "translation": {
                    "provider_id": "groq",
                    "model_id": "llama-3.3-70b-versatile",
                    "prompt": "translate only",
                },
            },
        })
        validated = validate_workflow_config(config.workflows)

        self.assertEqual(validated["rewrite"].custom_endpoint,
                         "https://proxy.example/v1")
        self.assertEqual(validated["translation"].provider_id, "groq")
        self.assertNotEqual(
            validated["rewrite"].prompt, validated["translation"].prompt
        )
        public = config.diagnostic_mapping()
        self.assertNotIn("prompt", json.dumps(public))
        self.assertNotIn("api_key", json.dumps(public))

    def test_legacy_flat_settings_save_updates_nested_compatibility_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            repository = LocalConfigRepository(
                path, secret_store=MemorySecretStore())
            repository.save({
                "transcription_provider": "openai",
                "openai_audio_model": "whisper-1",
                "refinement_provider": "openai",
                "refinement_model": "gpt-4o-mini",
            })
            independent = repository.load().to_legacy_mapping()
            independent["workflows"]["translation"] = {
                "provider_id": "groq",
                "model_id": "llama-3.3-70b-versatile",
                "prompt": "translate independently",
                "custom_endpoint": "https://proxy.example/v1",
            }
            repository.save(independent)
            repository.save({"ui_language": "pt"})
            preserved = repository.load().workflow(WorkflowScope.TRANSLATION)
            self.assertEqual(preserved.provider_id, "groq")
            self.assertEqual(preserved.custom_endpoint,
                             "https://proxy.example/v1")

            legacy = repository.load().to_legacy_mapping()
            legacy.update({
                "transcription_provider": "groq",
                "groq_audio_model": "whisper-large-v3",
                "refinement_provider": "groq",
                "refinement_model": "llama-3.3-70b-versatile",
                "local_asr_cloud_refinement": True,
            })
            repository.apply(legacy)

            loaded = repository.load()
            self.assertEqual(
                loaded.workflow(WorkflowScope.TRANSCRIPTION).provider_id,
                "groq",
            )
            self.assertEqual(
                loaded.workflow(WorkflowScope.REFINEMENT).model_id,
                "llama-3.3-70b-versatile",
            )
            self.assertEqual(
                loaded.workflow(WorkflowScope.REWRITE).provider_id,
                "groq",
            )
            self.assertTrue(loaded.workflow(
                WorkflowScope.LOCAL_ASR_REFINEMENT).enabled)

    def test_registry_applies_endpoint_override_before_adapter_work(self):
        connection = ProviderConnection("test-key", "https://api.example/v1")
        routed = PROVIDER_REGISTRY.connection_for_route(
            "openai", connection, "https://proxy.example/v1"
        )
        self.assertEqual(routed.api_key, "test-key")
        self.assertEqual(routed.base_url, "https://proxy.example/v1")
        with self.assertRaises(UnsupportedCapabilityError):
            PROVIDER_REGISTRY.connection_for_route(
                "local_asr", connection, "https://proxy.example"
            )

    def test_scoped_configuration_test_ignores_unrelated_invalid_routes(self):
        workflows = AppConfig.from_mapping({
            "workflows": {
                "translation": {
                    "provider_id": "groq",
                    "model_id": "llama-3.3-70b-versatile",
                },
                "rewrite": {
                    "provider_id": "local_asr",
                    "model_id": "ggml-small",
                },
            },
        }).workflows
        result = test_workflow_configuration(workflows, "translation")
        self.assertTrue(result.ok)
        self.assertEqual(result.scope, "translation")
        with self.assertRaises(WorkflowConfigurationError) as raised:
            test_workflow_configuration(workflows)
        self.assertEqual(raised.exception.scope, "rewrite")

    def test_diagnostics_reject_or_redact_endpoint_credentials(self):
        with self.assertRaises(WorkflowConfigurationError):
            validate_workflow_config(AppConfig.from_mapping({
                "workflows": {
                    "rewrite": {
                        "provider_id": "openai",
                        "custom_endpoint": "https://user:password@proxy.example/v1",
                    },
                },
            }).workflows)
        with self.assertRaises(WorkflowConfigurationError):
            validate_workflow_config(AppConfig.from_mapping({
                "workflows": {
                    "rewrite": {
                        "provider_id": "openai",
                        "custom_endpoint": "https://proxy.example/v1?api_key=secret",
                    },
                },
            }).workflows)

        config = AppConfig.from_mapping({
            "workflows": {
                "rewrite": {
                    "provider_id": "openai",
                    "custom_endpoint": "https://proxy.example/v1?region=eu",
                },
            },
        })
        diagnostic = config.diagnostic_mapping()
        self.assertEqual(
            diagnostic["workflows"]["rewrite"]["custom_endpoint"],
            "https://proxy.example/v1",
        )
        self.assertNotIn("region", json.dumps(diagnostic))
        result = test_workflow_configuration(config.workflows, "rewrite")
        self.assertNotIn("region", json.dumps(result.to_mapping()))

    def test_capability_errors_are_actionable_before_provider_work(self):
        workflows = AppConfig.from_mapping({
            "workflows": {
                "rewrite": {
                    "provider_id": "local_asr",
                    "model_id": "ggml-small",
                },
            },
        }).workflows
        with self.assertRaises(WorkflowConfigurationError) as raised:
            validate_workflow_config(workflows)
        self.assertEqual(raised.exception.scope, "rewrite")
        self.assertEqual(raised.exception.provider_id, "local_asr")
        self.assertIs(raised.exception.capability, ProviderCapability.TEXT_GENERATION)
        self.assertEqual(raised.exception.field, "provider_id")

        with self.assertRaises(WorkflowConfigurationError) as endpoint_error:
            validate_workflow_config(
                workflows.with_route(
                    "transcription",
                    WorkflowRoute(
                        provider_id="local_asr",
                        model_id="ggml-small",
                        custom_endpoint="https://proxy.example",
                    ),
                )
            )
        self.assertEqual(endpoint_error.exception.field, "custom_endpoint")

    def test_repository_apply_rolls_back_file_and_secret_on_persistence_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            secrets = MemorySecretStore({"openai": "old-test-key"})
            repository = LocalConfigRepository(path, secret_store=secrets)
            repository.save({"openai_api_key": "old-test-key"})
            before = path.read_bytes()

            candidate = repository.load().to_mapping()
            candidate["openai_api_key"] = "new-test-key"
            candidate["workflows"]["translation"] = {
                "provider_id": "groq",
                "model_id": "llama-3.3-70b-versatile",
                "prompt": "translate",
            }
            with patch(
                "repositories._atomic_write_json",
                side_effect=OSError("simulated settings write failure"),
            ):
                with self.assertRaises(OSError):
                    repository.apply(candidate)

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(secrets.get("openai"), "old-test-key")

    def test_reset_and_test_are_local_and_preserve_other_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            repository = LocalConfigRepository(path, secret_store=MemorySecretStore())
            current = repository.load()
            candidate = AppConfig.from_mapping({
                **current.to_mapping(),
                "workflows": {
                    **current.workflows.to_mapping(),
                    "rewrite": {
                        "provider_id": "groq",
                        "model_id": "llama-3.3-70b-versatile",
                        "prompt": "custom rewrite",
                    },
                },
            })
            repository.apply(candidate)
            result = repository.test_workflow(WorkflowScope.TRANSLATION)

            self.assertTrue(result.ok)
            self.assertEqual(result.scope, "translation")
            self.assertEqual(result.capability, "text_generation")
            reset = repository.reset_workflow("rewrite")
            self.assertEqual(reset.workflow("rewrite").provider_id, "openai")
            self.assertNotEqual(
                reset.workflow("rewrite").prompt, "custom rewrite"
            )
            self.assertEqual(
                repository.load().workflow("translation").provider_id,
                "openai",
            )


if __name__ == "__main__":
    unittest.main()
