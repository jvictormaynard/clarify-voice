import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from provider_adapters import GeminiAdapter, OpenAICompatibleAdapter
from provider_http import InvalidResponseError
from provider_registry import ProviderRegistry, build_provider_registry
from provider_types import (
    ModelCatalog,
    ProviderCapability,
    ProviderConfigurationError,
    ProviderConnection,
    ProviderMetadata,
    RewriteRequest,
    TranscriptionResult,
    TranscriptionRequest,
    TranslationRequest,
    UnsupportedCapabilityError,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self.payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.events = []

    def _request(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected {method} request to {url}")
        return self.responses.pop(0)

    def request(self, method, url, **kwargs):
        return self._request(method.upper(), url, kwargs)

    @staticmethod
    def json(response, **_kwargs):
        return response.json()

    def invalid_response(self, response, *, provider, operation):
        error = InvalidResponseError(
            provider=provider,
            operation=operation,
            status_code=response.status_code,
            operation_id=getattr(response, "_clarify_operation_id", None),
        )
        self.events.append({
            "event": "provider_http_error",
            "provider": provider,
            "operation": operation,
            "operation_id": error.operation_id,
            "status_code": error.status_code,
            "error_type": error.code,
        })
        return error


def compatible_metadata(provider_id="compatible", capabilities=None):
    return ProviderMetadata(
        provider_id=provider_id,
        display_name=provider_id.title(),
        capabilities=frozenset(capabilities or {
            ProviderCapability.AUDIO_TRANSCRIPTION,
            ProviderCapability.TEXT_GENERATION,
            ProviderCapability.MODEL_DISCOVERY,
            ProviderCapability.CUSTOM_BASE_URL,
        }),
        default_base_url="https://compatible.example/v1",
        audio_model_key=f"{provider_id}_audio_model",
        text_model_key=f"{provider_id}_text_model",
        default_audio_model="whisper-compatible",
        default_text_model="chat-compatible",
    )


class ProviderRegistryContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.audio_path = Path(self.directory.name) / "sample.wav"
        self.audio_path.write_bytes(b"RIFFfake-wave")

    def tearDown(self):
        self.directory.cleanup()

    def test_default_registry_is_authoritative_for_ids_metadata_and_capabilities(self):
        http = FakeHttp()
        registry = build_provider_registry(http)

        self.assertEqual(
            registry.provider_ids, ("gemini", "openai", "groq", "local_asr"))
        self.assertEqual(registry.describe("openai").display_name, "OpenAI")
        self.assertEqual(
            registry.describe("groq").audio_model_key, "groq_audio_model")
        self.assertTrue(registry.supports(
            "gemini", ProviderCapability.MULTIMODAL_AUDIO))
        self.assertFalse(registry.supports(
            "groq", ProviderCapability.MULTIMODAL_AUDIO))
        self.assertTrue(registry.supports(
            "local_asr", ProviderCapability.AUDIO_TRANSCRIPTION))
        self.assertFalse(registry.supports(
            "local_asr", ProviderCapability.TEXT_GENERATION))
        for provider_id in registry.provider_ids:
            with self.subTest(provider_id=provider_id):
                adapter = registry.adapter(provider_id)
                if provider_id == "local_asr":
                    self.assertEqual(adapter.metadata.provider_id, provider_id)
                else:
                    self.assertIs(adapter.http, http)

    def test_local_prompt_does_not_fallback_to_cloud_without_opt_in(self):
        original = app.APP_CONFIG.copy()
        try:
            app.APP_CONFIG["transcription_provider"] = "local_asr"
            app.APP_CONFIG["local_asr_model"] = "ggml-small"
            app.APP_CONFIG["local_asr_cloud_refinement"] = False
            with patch.object(
                    app.PROVIDER_REGISTRY, "transcribe",
                    return_value=TranscriptionResult(
                        "local transcript", "local_asr", "ggml-small")) as transcribe, \
                    patch.object(app, "_refine_transcript") as refine:
                result = app._call_provider_audio(
                    "local_asr", self.audio_path, "prompt", "en",
                    audio_bytes=b"RIFF")
            self.assertEqual(result, "local transcript")
            transcribe.assert_called_once()
            refine.assert_not_called()
        finally:
            app.APP_CONFIG.clear()
            app.APP_CONFIG.update(original)

    def test_gemini_contract_discovers_canonical_ids_and_uses_custom_proxy_auth(self):
        http = FakeHttp(
            FakeResponse({"models": [
                {"name": "models/gemini-3-flash",
                 "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/embed-1",
                 "supportedGenerationMethods": ["embedContent"]},
            ]}),
            FakeResponse({
                "candidates": [{"content": {"parts": [{"text": "hello"}]}}],
            }),
        )
        registry = build_provider_registry(http)
        connection = ProviderConnection("proxy-key", "https://proxy.example")

        catalog = registry.discover_models("gemini", connection)
        result = registry.transcribe("gemini", TranscriptionRequest(
            audio_path=self.audio_path,
            model="models/gemini-3-flash",
            language="en",
            instruction="Transcribe faithfully.",
            prompt="Transcribe this audio.",
            temperature=0.0,
        ), connection)

        self.assertEqual(catalog, ModelCatalog(
            ("gemini-3-flash",), ("gemini-3-flash",)))
        self.assertEqual(result.text, "hello")
        self.assertEqual(result.model, "gemini-3-flash")
        self.assertEqual(
            http.calls[0][1], "https://proxy.example/v1beta/models")
        self.assertEqual(http.calls[0][2]["headers"], {
            "x-goog-api-key": "proxy-key",
            "Authorization": "Bearer proxy-key",
        })
        self.assertEqual(
            http.calls[1][1],
            "https://proxy.example/v1beta/models/gemini-3-flash:generateContent",
        )
        self.assertEqual(http.calls[0][2]["operation"], "model_discovery")
        self.assertEqual(http.calls[1][2]["operation"], "transcription")
        self.assertFalse(http.calls[1][2]["safe_to_retry"])
        self.assertNotIn("timeout", http.calls[0][2])
        self.assertNotIn("timeout", http.calls[1][2])

    def test_openai_compatible_contract_filters_catalog_and_uses_api_ids(self):
        http = FakeHttp(
            FakeResponse({"data": [
                {"id": "whisper-compatible", "name": "Pretty Whisper"},
                {"id": "chat-compatible", "name": "Pretty Chat"},
                {"id": "text-embedding-compatible"},
            ]}),
            FakeResponse({"text": "raw transcript"}),
            FakeResponse({"choices": [{"message": {"content": "rewritten"}}]}),
        )
        registry = ProviderRegistry()
        registry.register_openai_compatible(
            compatible_metadata(), http,
            audio_model_aliases={"Pretty Whisper": "whisper-compatible"},
        )
        connection = ProviderConnection("key", "https://proxy.example/v1")

        catalog = registry.discover_models("compatible", connection)
        transcript = registry.transcribe("compatible", TranscriptionRequest(
            self.audio_path, "Pretty Whisper", "pt", "unused", "unused", 0.0,
        ), connection)
        rewrite = registry.rewrite("compatible", RewriteRequest(
            "raw transcript", "chat-compatible", "pt", "Rewrite.",
            "SOURCE", 0.1,
        ), connection)

        self.assertEqual(catalog.audio_models, ("whisper-compatible",))
        self.assertEqual(catalog.text_models, ("chat-compatible",))
        self.assertEqual(transcript.model, "whisper-compatible")
        self.assertEqual(rewrite.text, "rewritten")
        self.assertEqual(
            http.calls[1][1], "https://proxy.example/v1/audio/transcriptions")
        self.assertEqual(
            http.calls[1][2]["data"]["model"], "whisper-compatible")
        self.assertEqual(
            http.calls[2][1], "https://proxy.example/v1/chat/completions")
        self.assertEqual(http.calls[0][2]["operation"], "model_discovery")
        self.assertEqual(http.calls[1][2]["operation"], "transcription")
        self.assertEqual(http.calls[2][2]["operation"], "text_generation")
        self.assertFalse(http.calls[1][2]["safe_to_retry"])
        self.assertFalse(http.calls[2][2]["safe_to_retry"])
        for _method, _url, kwargs in http.calls:
            self.assertNotIn("timeout", kwargs)

    def test_audio_upload_uses_snapshot_without_opening_recording_path(self):
        http = FakeHttp(FakeResponse({"text": "snapshot transcript"}))
        uploaded = []
        original_request = http.request

        def capture_request(method, url, **kwargs):
            uploaded.append(kwargs["files"]["file"][1].read())
            return original_request(method, url, **kwargs)

        http.request = capture_request
        registry = ProviderRegistry()
        registry.register_openai_compatible(compatible_metadata(), http)
        request = TranscriptionRequest(
            self.audio_path, "whisper-compatible", "en", "unused", "unused",
            0.0, audio_bytes=b"snapshot-bytes")
        self.audio_path.unlink()

        result = registry.transcribe(
            "compatible", request, ProviderConnection("key", "https://proxy.example/v1"))

        self.assertEqual(result.text, "snapshot transcript")
        self.assertEqual(uploaded, [b"snapshot-bytes"])

    def test_compatible_provider_routes_without_changes_to_workflow_code(self):
        http = FakeHttp(
            FakeResponse({"text": "transcribed"}),
            FakeResponse({"choices": [{"message": {"content": "translated"}}]}),
        )
        registry = ProviderRegistry()
        registry.register_openai_compatible(compatible_metadata("acme"), http)
        connection = ProviderConnection("key", "https://acme.example")

        transcription = registry.transcribe("acme", TranscriptionRequest(
            self.audio_path, "whisper-acme", "en", "unused", "unused", 0.0,
        ), connection)
        translation = registry.translate("acme", TranslationRequest(
            "Hello", "chat-acme", "de", "Translate to German.", "SOURCE", 0.0,
        ), connection)

        self.assertEqual(transcription.text, "transcribed")
        self.assertEqual(translation.text, "translated")
        self.assertEqual(translation.target_language, "de")

    def test_openai_compatible_text_operations_accept_non_empty_strings(self):
        http = FakeHttp(
            FakeResponse({
                "choices": [{"message": {"content": "  rewritten  "}}],
            }),
            FakeResponse({
                "choices": [{"message": {"content": "  translated  "}}],
            }),
        )
        registry = ProviderRegistry()
        registry.register_openai_compatible(compatible_metadata(), http)
        connection = ProviderConnection("key", "https://compatible.example/v1")

        rewrite = registry.rewrite("compatible", RewriteRequest(
            "Raw", "chat-compatible", "en", "Rewrite.", "SOURCE", 0.1,
        ), connection)
        translation = registry.translate("compatible", TranslationRequest(
            "Hello", "chat-compatible", "de", "Translate.", "SOURCE", 0.0,
        ), connection)

        self.assertEqual(rewrite.text, "rewritten")
        self.assertEqual(translation.text, "translated")

    def test_openai_compatible_text_operations_reject_invalid_content(self):
        requests = (
            ("rewrite", RewriteRequest(
                "Raw", "chat-compatible", "en", "Rewrite.", "SOURCE", 0.1,
            )),
            ("translate", TranslationRequest(
                "Hello", "chat-compatible", "de", "Translate.", "SOURCE", 0.0,
            )),
        )

        for operation, request in requests:
            for content in (None, [], {}, "   "):
                with self.subTest(operation=operation, content=content):
                    http = FakeHttp(FakeResponse({
                        "choices": [{"message": {"content": content}}],
                    }))
                    registry = ProviderRegistry()
                    registry.register_openai_compatible(
                        compatible_metadata(), http)

                    with self.assertRaises(InvalidResponseError) as raised:
                        getattr(registry, operation)(
                            "compatible", request,
                            ProviderConnection(
                                "key", "https://compatible.example/v1"),
                        )

                    self.assertEqual(
                        raised.exception.capability,
                        ProviderCapability.TEXT_GENERATION,
                    )

    def test_malformed_success_responses_keep_ids_and_emit_diagnostic_events(self):
        connection = ProviderConnection("key", "https://compatible.example/v1")

        cases = (
            (
                "transcription",
                {"text": "   "},
                lambda registry: registry.transcribe(
                    "compatible", TranscriptionRequest(
                        self.audio_path, "whisper-compatible", "en",
                        "unused", "unused", 0.0), connection),
            ),
            (
                "text_generation",
                {"choices": [{"message": {"content": "   "}}]},
                lambda registry: registry.rewrite(
                    "compatible", RewriteRequest(
                        "Raw", "chat-compatible", "en", "Rewrite.",
                        "SOURCE", 0.1), connection),
            ),
            (
                "model_discovery",
                {"data": {"not": "a list"}},
                lambda registry: registry.discover_models(
                    "compatible", connection),
            ),
        )

        for operation, payload, invoke in cases:
            with self.subTest(operation=operation):
                response = FakeResponse(payload)
                response._clarify_operation_id = f"{operation}-123"
                http = FakeHttp(response)
                registry = ProviderRegistry()
                registry.register_openai_compatible(compatible_metadata(), http)

                with self.assertRaises(InvalidResponseError) as raised:
                    invoke(registry)

                self.assertEqual(raised.exception.operation_id,
                                 f"{operation}-123")
                self.assertEqual(len(http.events), 1)
                self.assertEqual(http.events[0], {
                    "event": "provider_http_error",
                    "provider": "compatible",
                    "operation": operation,
                    "operation_id": f"{operation}-123",
                    "status_code": 200,
                    "error_type": "invalid_response",
                })

    def test_desktop_workflow_routes_a_registered_compatible_provider(self):
        http = FakeHttp(FakeResponse({"text": "desktop transcript"}))
        registry = ProviderRegistry()
        registry.register_openai_compatible(compatible_metadata("acme"), http)
        config = {
            "transcription_provider": "acme",
            "acme_api_key": "key",
            "acme_base_url": "https://acme.example",
            "acme_audio_model": "whisper-acme",
        }

        with patch.object(app, "PROVIDER_REGISTRY", registry), patch.dict(
                app.APP_CONFIG, config, clear=False):
            result = app.call_transcription_provider(
                self.audio_path, "transcription", "en")

        self.assertEqual(result, "desktop transcript")
        self.assertEqual(
            http.calls[0][1], "https://acme.example/v1/audio/transcriptions")

    def test_unsupported_capability_is_typed_and_actionable(self):
        registry = ProviderRegistry()
        metadata = compatible_metadata(
            "textonly", {ProviderCapability.TEXT_GENERATION})
        registry.register(OpenAICompatibleAdapter(metadata, FakeHttp()))

        with self.assertRaises(UnsupportedCapabilityError) as raised:
            registry.transcribe("textonly", TranscriptionRequest(
                self.audio_path, "model", "en", "unused", "unused", 0.0,
            ), ProviderConnection("key", "https://text.example"))

        self.assertEqual(
            raised.exception.capability,
            ProviderCapability.AUDIO_TRANSCRIPTION,
        )
        self.assertIn("does not support audio transcription", str(raised.exception))
        self.assertIn("Choose a provider", str(raised.exception))

    def test_missing_credentials_raise_typed_configuration_error(self):
        registry = build_provider_registry(FakeHttp())

        with self.assertRaises(ProviderConfigurationError) as raised:
            registry.validate("gemini", ProviderConnection(
                "", "https://generativelanguage.googleapis.com/v1beta"))

        self.assertEqual(raised.exception.provider_id, "gemini")
        self.assertIn("API key", str(raised.exception))

    def test_provider_layer_never_imports_tk_or_ui_modules(self):
        for filename in (
                "provider_types.py", "provider_adapters.py", "provider_registry.py"):
            source = (ROOT / filename).read_text(encoding="utf-8").lower()
            self.assertNotIn("tkinter", source, filename)
            self.assertNotIn("customtkinter", source, filename)
            self.assertNotIn("from app", source, filename)


if __name__ == "__main__":
    unittest.main()
