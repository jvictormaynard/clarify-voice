"""Focused contracts for workflow-scoped provider configuration (#51)."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
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
    UnsupportedSchemaVersionError,
    WorkflowConfig,
    WorkflowConfigurationError,
    WorkflowRoute,
    WorkflowScope,
    migrate_config_payload,
    test_workflow_configuration,
    validate_workflow_config,
)
from secret_store import MemorySecretStore


class WorkflowConfigurationTests(unittest.TestCase):
    def test_migration_route_model_default_uses_route_provider(self):
        migrated = migrate_config_payload({
            "schema_version": 1,
            "refinement_provider": "openai",
            "refinement_model": "gpt-4o-mini",
            "workflows": {
                "translation": {"provider_id": "groq"},
            },
        })

        route = migrated["workflows"]["translation"]
        self.assertEqual(route["provider_id"], "groq")
        self.assertEqual(route["model_id"], "llama-3.3-70b-versatile")

    def test_migration_canonicalizes_scope_aliases_before_defaults(self):
        migrated = migrate_config_payload({
            "schema_version": 1,
            "transcription_provider": "gemini",
            "gemini_model": "gemini-2.5-flash",
            "workflows": {
                "dictation": {"provider_id": "groq"},
            },
        })

        route = migrated["workflows"]["transcription"]
        self.assertEqual(route["provider_id"], "groq")
        self.assertEqual(route["model_id"], "whisper-large-v3-turbo")
        self.assertNotIn("dictation", migrated["workflows"])

    def test_workflow_config_from_mapping_normalizes_all_scope_aliases(self):
        workflows = WorkflowConfig.from_mapping({
            "dictation": {
                "provider_id": "groq",
                "model_id": "whisper-large-v3",
            },
            "text_refinement": {
                "provider_id": "groq",
                "model_id": "llama-3.3-70b-versatile",
            },
            "local-refinement": {
                "provider_id": "openai",
                "model_id": "gpt-4o-mini",
            },
        })

        self.assertEqual(workflows.transcription.provider_id, "groq")
        self.assertEqual(workflows.refinement.provider_id, "groq")
        self.assertEqual(workflows.local_asr_refinement.provider_id, "openai")

        invalid = WorkflowConfig.from_mapping({
            "dictation": {"provider_id": "not-registered"},
        })
        self.assertEqual(invalid.transcription.provider_id, "not-registered")
        with self.assertRaises(WorkflowConfigurationError):
            validate_workflow_config(invalid)

    def test_app_config_compatibility_fields_follow_scope_aliases(self):
        config = AppConfig.from_mapping({
            "workflows": {
                "dictation": {
                    "provider_id": "groq",
                    "model_id": "whisper-large-v3",
                },
                "text_refinement": {
                    "provider_id": "groq",
                    "model_id": "llama-3.3-70b-versatile",
                },
                "local-refinement": {
                    "provider_id": "openai",
                    "model_id": "gpt-4o-mini",
                    "enabled": True,
                },
            },
        })

        self.assertEqual(config.selection.transcription_provider, "groq")
        self.assertEqual(config.selection.refinement_provider, "groq")
        self.assertEqual(
            config.selection.refinement_model, "llama-3.3-70b-versatile"
        )
        self.assertTrue(config.local_asr_cloud_refinement)
        legacy = config.to_legacy_mapping()
        self.assertEqual(legacy["transcription_provider"], "groq")
        self.assertEqual(legacy["refinement_provider"], "groq")
        self.assertTrue(legacy["local_asr_cloud_refinement"])

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
            independent["workflows"]["rewrite"] = {
                "provider_id": "openai",
                "model_id": "gpt-4o-mini",
                "prompt": "rewrite independently",
                "custom_endpoint": "https://rewrite-proxy.example/v1",
            }
            independent["workflows"]["refinement"][
                "custom_endpoint"] = "https://refinement-proxy.example/v1"
            repository.save(independent)
            repository.save({
                "workflows": {
                    "translation": {"prompt": "partial translation"},
                },
            })
            repository.save({"ui_language": "pt"})
            preserved = repository.load().workflow(WorkflowScope.TRANSLATION)
            self.assertEqual(preserved.provider_id, "groq")
            self.assertEqual(preserved.custom_endpoint,
                             "https://proxy.example/v1")
            self.assertEqual(preserved.prompt, "partial translation")

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
                "openai",
            )
            self.assertEqual(
                loaded.workflow(WorkflowScope.REWRITE).custom_endpoint,
                "https://rewrite-proxy.example/v1",
            )
            self.assertEqual(
                loaded.workflow(WorkflowScope.REFINEMENT).custom_endpoint,
                "",
            )
            self.assertTrue(loaded.workflow(
                WorkflowScope.LOCAL_ASR_REFINEMENT).enabled)

    def test_transcription_provider_change_clears_endpoint_but_model_change_keeps_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            repository = LocalConfigRepository(
                path, secret_store=MemorySecretStore())
            repository.save({
                "transcription_provider": "openai",
                "openai_audio_model": "whisper-1",
                "workflows": {
                    "transcription": {
                        "provider_id": "openai",
                        "model_id": "whisper-1",
                        "custom_endpoint": "https://openai-proxy.example/v1",
                    },
                },
            })

            model_change = repository.load().to_legacy_mapping()
            model_change["openai_audio_model"] = "gpt-4o-transcribe"
            repository.save(model_change)
            self.assertEqual(
                repository.load().workflow(WorkflowScope.TRANSCRIPTION).custom_endpoint,
                "https://openai-proxy.example/v1",
            )

            provider_change = repository.load().to_legacy_mapping()
            provider_change.update({
                "transcription_provider": "groq",
                "groq_audio_model": "whisper-large-v3",
            })
            repository.save(provider_change)
            self.assertEqual(
                repository.load().workflow(WorkflowScope.TRANSCRIPTION).custom_endpoint,
                "",
            )

    def test_refinement_model_change_keeps_custom_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            repository = LocalConfigRepository(
                path, secret_store=MemorySecretStore())
            repository.save({
                "refinement_provider": "openai",
                "refinement_model": "gpt-4o-mini",
                "workflows": {
                    "refinement": {
                        "provider_id": "openai",
                        "model_id": "gpt-4o-mini",
                        "custom_endpoint": "https://proxy.example/v1",
                    },
                },
            })

            changed = repository.load().to_legacy_mapping()
            changed["refinement_model"] = "gpt-4o"
            repository.save(changed)

            route = repository.load().workflow(WorkflowScope.REFINEMENT)
            self.assertEqual(route.model_id, "gpt-4o")
            self.assertEqual(route.custom_endpoint, "https://proxy.example/v1")

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

    def test_direct_repository_save_validates_workflow_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            repository = LocalConfigRepository(
                path, secret_store=MemorySecretStore())
            invalid_mappings = (
                {
                    "rewrite": {
                        "provider_id": "openai",
                        "custom_endpoint": (
                            "https://user:password@proxy.example/v1"
                        ),
                    },
                },
                {
                    "rewrite": {
                        "provider_id": "openai",
                        "custom_endpoint": "https://proxy.example:0/v1",
                    },
                },
                {
                    "transcription": {
                        "provider_id": "local_asr",
                        "model_id": "ggml-medium",
                    },
                },
            )
            for routes in invalid_mappings:
                with self.assertRaises(WorkflowConfigurationError):
                    repository.save({"workflows": routes})

            typed = AppConfig.from_mapping({
                "workflows": {
                    "rewrite": {
                        "provider_id": "openai",
                        "custom_endpoint": "https://proxy.example:0/v1",
                    },
                },
            })
            with self.assertRaises(WorkflowConfigurationError):
                repository.save(typed)
            self.assertFalse(path.exists())

    def test_legacy_app_save_refreshes_nested_routes_for_later_saves(self):
        import app

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            repository = LocalConfigRepository(
                path, secret_store=MemorySecretStore())
            storage = SimpleNamespace(config=repository)
            initial = repository.load().to_legacy_mapping()
            with patch.object(app, "APP_CONFIG", initial):
                app.APP_CONFIG.update({
                    "refinement_provider": "groq",
                    "refinement_model": "llama-3.3-70b-versatile",
                })
                app._save_app_config(storage)
                self.assertEqual(
                    app.APP_CONFIG["workflows"]["refinement"]["provider_id"],
                    "groq",
                )
                app.APP_CONFIG["ui_language"] = "pt"
                app._save_app_config(storage)

            loaded = repository.load()
            self.assertEqual(
                loaded.workflow(WorkflowScope.REWRITE).provider_id,
                "groq",
            )

    def test_mapping_apply_preserves_explicit_refinement_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            repository = LocalConfigRepository(
                path, secret_store=MemorySecretStore())
            candidate = repository.load().to_mapping()
            candidate["refinement_provider"] = "groq"
            candidate["refinement_model"] = "llama-3.3-70b-versatile"
            candidate["workflows"]["refinement"] = {
                "provider_id": "groq",
                "model_id": "llama-3.3-70b-versatile",
                "custom_endpoint": "https://refinement-proxy.example/v1",
            }

            applied = repository.apply(candidate)

            self.assertEqual(
                applied.workflow(WorkflowScope.REFINEMENT).custom_endpoint,
                "https://refinement-proxy.example/v1",
            )
            self.assertEqual(
                repository.load().workflow(WorkflowScope.REFINEMENT).custom_endpoint,
                "https://refinement-proxy.example/v1",
            )

    def test_mapping_apply_preserves_endpoint_alias_and_scope_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            repository = LocalConfigRepository(
                path, secret_store=MemorySecretStore())
            candidate = repository.load().to_mapping()
            candidate["refinement_provider"] = "groq"
            candidate["refinement_model"] = "llama-3.3-70b-versatile"
            # The alias is the sole refinement route in this partial payload;
            # canonical and alias spellings are intentionally not mixed.
            candidate["workflows"].pop("refinement")
            candidate["workflows"]["cleanup"] = {
                "provider_id": "groq",
                "model_id": "llama-3.3-70b-versatile",
                "base_url": "https://refinement-alias-proxy.example/v1",
            }

            applied = repository.apply(candidate)

            self.assertEqual(
                applied.workflow(WorkflowScope.REFINEMENT).custom_endpoint,
                "https://refinement-alias-proxy.example/v1",
            )
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("cleanup", persisted["workflows"])

    def test_mapping_merge_prefers_canonical_scope_over_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            repository = LocalConfigRepository(
                path, secret_store=MemorySecretStore())
            candidate = repository.load().to_mapping()
            candidate["workflows"]["cleanup"] = {
                "provider_id": "groq",
                "model_id": "llama-3.3-70b-versatile",
                "prompt": "alias route",
            }
            candidate["workflows"]["refinement"] = {
                "provider_id": "openai",
                "model_id": "gpt-4o-mini",
                "prompt": "canonical route",
            }

            applied = repository.apply(candidate)

            self.assertEqual(
                applied.workflow(WorkflowScope.REFINEMENT).provider_id,
                "openai",
            )
            self.assertEqual(
                applied.workflow(WorkflowScope.REFINEMENT).prompt,
                "canonical route",
            )

    def test_partial_save_normalizes_scope_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            repository = LocalConfigRepository(
                path, secret_store=MemorySecretStore())

            repository.save({
                "workflows": {
                    "cleanup": {"prompt": "alias refinement"},
                },
            })

            loaded = repository.load()
            self.assertEqual(
                loaded.workflow(WorkflowScope.REFINEMENT).prompt,
                "alias refinement",
            )
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("cleanup", persisted["workflows"])

    def test_partial_save_migrates_v1_routes_before_merging(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "refinement_provider": "openai",
                "refinement_model": "gpt-4o-mini",
                "workflows": {
                    "translation": {"provider_id": "groq"},
                },
            }), encoding="utf-8")
            repository = LocalConfigRepository(
                path, secret_store=MemorySecretStore())

            repository.save({"ui_language": "pt"})

            route = repository.load().workflow(WorkflowScope.TRANSLATION)
            self.assertEqual(route.provider_id, "groq")
            self.assertEqual(route.model_id, "llama-3.3-70b-versatile")

    def test_apply_rejects_future_schema_mapping_before_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            repository = LocalConfigRepository(
                path, secret_store=MemorySecretStore())

            with self.assertRaises(UnsupportedSchemaVersionError):
                repository.apply({
                    "schema_version": CONFIG_SCHEMA_VERSION + 1,
                    "future_setting": "must remain untouched",
                })

            self.assertFalse(path.exists())

    def test_mapping_apply_blank_model_uses_new_provider_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            repository = LocalConfigRepository(
                path, secret_store=MemorySecretStore())
            candidate = repository.load().to_mapping()
            candidate["refinement_provider"] = "groq"
            candidate["refinement_model"] = ""
            candidate["workflows"]["refinement"] = {
                "provider_id": "groq",
                "model_id": "",
            }

            applied = repository.apply(candidate)

            self.assertEqual(
                applied.workflow(WorkflowScope.REFINEMENT).model_id,
                "llama-3.3-70b-versatile",
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
        for query_name in ("x-api-key", "subscription-key"):
            with self.assertRaises(WorkflowConfigurationError):
                validate_workflow_config(AppConfig.from_mapping({
                    "workflows": {
                        "rewrite": {
                            "provider_id": "openai",
                            "custom_endpoint": (
                                f"https://proxy.example/v1?{query_name}=secret"
                            ),
                        },
                    },
                }).workflows)
        for port in ("0", "notaport", "99999"):
            with self.assertRaises(WorkflowConfigurationError):
                validate_workflow_config(AppConfig.from_mapping({
                    "workflows": {
                        "rewrite": {
                            "provider_id": "openai",
                            "custom_endpoint": (
                                f"https://proxy.example:{port}/v1"
                            ),
                        },
                    },
                }).workflows)
        for hostname in (
                "proxy example", "proxy..example", "-proxy.example",
                "proxy-.example", "999.999.999.999"):
            with self.assertRaises(WorkflowConfigurationError):
                validate_workflow_config(AppConfig.from_mapping({
                    "workflows": {
                        "rewrite": {
                            "provider_id": "openai",
                            "custom_endpoint": f"https://{hostname}/v1",
                        },
                    },
                }).workflows)
        for suffix in ("?region=eu", "#fragment", "?", "#"):
            with self.assertRaises(WorkflowConfigurationError):
                validate_workflow_config(AppConfig.from_mapping({
                    "workflows": {
                        "rewrite": {
                            "provider_id": "openai",
                            "custom_endpoint": (
                                f"https://proxy.example/v1{suffix}"
                            ),
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
        with self.assertRaises(WorkflowConfigurationError):
            test_workflow_configuration(config.workflows, "rewrite")

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

        with self.assertRaises(WorkflowConfigurationError) as model_error:
            validate_workflow_config(AppConfig.from_mapping({
                "workflows": {
                    "transcription": {
                        "provider_id": "local_asr",
                        "model_id": "ggml-medium",
                    },
                },
            }).workflows)
        self.assertEqual(model_error.exception.provider_id, "local_asr")
        self.assertEqual(model_error.exception.field, "model_id")

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

    def test_partial_apply_returns_secret_restored_from_secure_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            secrets = MemorySecretStore()
            repository = LocalConfigRepository(path, secret_store=secrets)
            repository.save({"openai_api_key": "keep-test-key"})

            applied = repository.apply({"ui_language": "pt"})

            self.assertEqual(applied.ui.language, "pt")
            self.assertEqual(applied.openai.api_key, "keep-test-key")
            self.assertEqual(secrets.get("openai"), "keep-test-key")

    def test_typed_apply_returns_reloaded_selection_after_route_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            repository = LocalConfigRepository(
                path, secret_store=MemorySecretStore())
            current = repository.load()
            candidate = replace(
                current,
                workflows=current.workflows.with_route(
                    WorkflowScope.REFINEMENT,
                    WorkflowRoute(
                        provider_id="groq",
                        model_id="llama-3.3-70b-versatile",
                    ),
                ),
            )

            applied = repository.apply(candidate)

            self.assertEqual(applied.selection.refinement_provider, "groq")
            self.assertEqual(
                applied.workflow(WorkflowScope.REFINEMENT).provider_id,
                "groq",
            )

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
