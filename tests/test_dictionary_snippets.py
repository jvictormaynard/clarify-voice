import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
from dictionary_snippets import (
    DictionaryEntry,
    DictionarySnippetService,
    DictionarySnippets,
    DictionarySnippetsError,
    LocalDictionarySnippetsRepository,
    Snippet,
)
from provider_types import TranscriptionRequest
from provider_registry import build_provider_registry
from provider_http import redact_sensitive


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _FakeHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    @staticmethod
    def json(response, **_kwargs):
        return response.json()


class DictionarySnippetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "dictionary.json"
        self.repository = LocalDictionarySnippetsRepository(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def test_round_trip_preserves_unicode_punctuation_and_multiline_replacement(self):
        state = DictionarySnippets(
            dictionary=(DictionaryEntry(
                "João 🚀",
                pronunciation="zh-ow",
                aliases=("Joao", "ジョアン"),
            ),),
            snippets=(Snippet(
                ";meeting",
                "Agenda:\n1. Confirm scope\n2. Share notes",
            ),),
        )
        self.repository.save(state)

        loaded = LocalDictionarySnippetsRepository(self.path).load()

        self.assertEqual(loaded, state)
        self.assertIn("João 🚀", self.repository.export_json())
        self.assertIn("Agenda:\\n1. Confirm scope", self.repository.export_json())

    def test_unversioned_document_migrates_to_v1(self):
        self.path.write_text(json.dumps({
            "dictionary": [{"term": "ClarifyVoice"}],
            "snippets": [{"trigger": "addr", "replacement": "address"}],
        }), encoding="utf-8")

        loaded = self.repository.load()

        self.assertEqual(loaded.schema_version, 1)
        self.assertEqual(loaded.dictionary[0].term, "ClarifyVoice")
        self.assertEqual(loaded.snippets[0].replacement, "address")

    def test_failed_import_leaves_previous_state_unchanged(self):
        original = DictionarySnippets(
            dictionary=(DictionaryEntry("OpenWhispr"),),
            snippets=(Snippet("brb", "be right back"),),
        )
        self.repository.save(original)
        service = DictionarySnippetService(self.repository)

        with self.assertRaises(DictionarySnippetsError):
            service.import_json({
                "schema_version": 1,
                "dictionary": [{"term": "OpenWhispr"}, {"term": "OpenWhispr"}],
                "snippets": [],
            })

        self.assertEqual(service.state, original)
        self.assertEqual(self.repository.load(), original)

    def test_malformed_json_and_future_schema_are_rejected_without_mutation(self):
        original = DictionarySnippets(snippets=(Snippet("sig", "Regards"),))
        self.repository.save(original)
        service = DictionarySnippetService(self.repository)

        for document in ("{not-json", {
            "schema_version": 99,
            "dictionary": [],
            "snippets": [],
        }, {
            "schema_version": "one",
            "dictionary": [],
            "snippets": [],
        }):
            with self.assertRaises(DictionarySnippetsError):
                service.import_json(document)

        self.assertEqual(service.state, original)
        self.assertEqual(self.repository.load(), original)

    def test_snippets_are_longest_first_and_respect_unicode_word_boundaries(self):
        service = DictionarySnippetService(self.repository)
        service.replace(DictionarySnippets(snippets=(
            Snippet("meet", "short"),
            Snippet("meet me", "long"),
            Snippet("café", "coffee", case_sensitive=False),
            Snippet("straße", "street", case_sensitive=False),
            Snippet("cafe", "base-only", case_sensitive=False),
        )))

        self.assertEqual(
            service.expand(
                "meet me, meet meeting café CAFÉ xmeet café-bar STRASSE "
                "cafe\u0301"),
            "long, short meeting coffee coffee xmeet coffee-bar street coffee",
        )

        base_only = DictionarySnippetService(self.repository)
        base_only.replace(DictionarySnippets(snippets=(Snippet("cafe", "base"),)))
        self.assertEqual(base_only.expand("cafe\u0301"), "cafe\u0301")

    def test_casefold_expansions_do_not_match_only_part_of_a_cluster(self):
        service = DictionarySnippetService(self.repository)
        service.replace(DictionarySnippets(snippets=(Snippet("s", "letter"),)))

        self.assertEqual(service.expand("ß"), "ß")

        service.replace(DictionarySnippets(snippets=(
            Snippet("s", "letter"),
            Snippet("ß", "sharp-s"),
        )))

        self.assertEqual(service.expand("ß"), "sharp-s")

    def test_case_sensitive_and_disabled_rules_are_explicit(self):
        service = DictionarySnippetService(self.repository)
        service.replace(DictionarySnippets(snippets=(
            Snippet("ID", "sensitive", case_sensitive=True),
            Snippet("todo", "done", enabled=False),
        )))

        self.assertEqual(service.expand("ID id todo"), "sensitive id todo")

    def test_empty_replacement_can_delete_a_bounded_trigger(self):
        service = DictionarySnippetService(self.repository)
        service.replace(DictionarySnippets(snippets=(Snippet("noise", ""),)))

        self.assertEqual(service.expand("keep noise."), "keep .")

    def test_context_is_bounded_ordered_and_request_never_requires_provider_branch(self):
        service = DictionarySnippetService(self.repository)
        service.replace(DictionarySnippets(dictionary=(
            DictionaryEntry("First"),
            DictionaryEntry("Second", aliases=("2nd",)),
        )))

        request = TranscriptionRequest(
            Path("audio.wav"), "model", "en", "instruction", "prompt", 0.0)
        contextual = service.apply_context(request, max_chars=250)

        self.assertIsNot(contextual, request)
        self.assertIn("First", contextual.dictionary_context)
        self.assertIn("Second", contextual.dictionary_context)
        self.assertNotIn("api_key", contextual.dictionary_context)
        self.assertEqual(request.dictionary_context, "")

        with self.assertRaises(ValueError):
            TranscriptionRequest(
                Path("audio.wav"), "model", "en", "instruction", "prompt", 0.0,
                dictionary_context="x" * 4097,
            ).effective_prompt()

    def test_expansion_rejects_unbounded_input(self):
        service = DictionarySnippetService(self.repository, max_expansion_chars=10)
        with self.assertRaises(DictionarySnippetsError):
            service.expand("12345678901")

        service.replace(DictionarySnippets(snippets=(Snippet("x", "0123456789"),)))
        with self.assertRaises(DictionarySnippetsError):
            service.expand("x x")

    def test_supported_cloud_adapters_forward_context_without_workflow_branches(self):
        audio_path = Path(self.directory.name) / "audio.wav"
        audio_path.write_bytes(b"RIFFfake")
        context = "Use the preferred term: ClarifyVoice."

        gemini_http = _FakeHttp(_FakeResponse({
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
        }))
        gemini_registry = build_provider_registry(gemini_http)
        gemini_registry.transcribe("gemini", TranscriptionRequest(
            audio_path, "gemini-2.5-flash", "en", "instruction", "prompt",
            0.0, dictionary_context=context,
        ), gemini_registry.connection_from_legacy("gemini", {
            "gemini_api_key": "key",
            "gemini_base_url": "https://proxy.example",
        }))
        self.assertIn(
            context,
            gemini_http.calls[0][2]["json"]["contents"][0]["parts"][1]["text"],
        )

        compatible_http = _FakeHttp(_FakeResponse({"text": "ok"}))
        compatible_registry = build_provider_registry(compatible_http)
        compatible_registry.transcribe("openai", TranscriptionRequest(
            audio_path, "whisper-1", "en", "instruction", "prompt", 0.0,
            dictionary_context=context,
        ), compatible_registry.connection_from_legacy("openai", {
            "openai_api_key": "key",
            "openai_base_url": "https://proxy.example/v1",
        }))
        self.assertEqual(
            compatible_http.calls[0][2]["data"]["prompt"],
            f"prompt\n\n{context}",
        )

    def test_dictionary_context_is_redacted_if_a_diagnostic_event_receives_it(self):
        context = "private project codename and pronunciation"
        redacted = redact_sensitive({"dictionary_context": context})
        self.assertEqual(redacted["dictionary_context"], "[REDACTED]")

    def test_recording_path_applies_context_and_expands_result_after_provider_work(self):
        service = DictionarySnippetService(self.repository)
        service.replace(DictionarySnippets(
            dictionary=(DictionaryEntry("ClarifyVoice"),),
            snippets=(Snippet("brb", "be right back"),),
        ))
        original_config = app.APP_CONFIG.copy()
        try:
            app.APP_CONFIG.update({
                "transcription_provider": "gemini",
                "gemini_api_key": "test-key",
                "gemini_base_url": "https://proxy.example",
                "gemini_model": "gemini-2.5-flash",
            })
            request_seen = []

            def transcribe(_provider, request, _connection, _cancel_token):
                request_seen.append(request)
                return type("Result", (), {
                    "text": "brb",
                })()

            with patch.object(app, "DICTIONARY_SERVICE", service), \
                    patch.object(app.PROVIDER_REGISTRY, "transcribe", transcribe):
                result = app._call_provider_audio(
                    "gemini", Path(self.directory.name) / "audio.wav", "transcription")

            self.assertEqual(result, "be right back")
            self.assertEqual(len(request_seen), 1)
            self.assertIn("ClarifyVoice", request_seen[0].dictionary_context)
        finally:
            app.APP_CONFIG.clear()
            app.APP_CONFIG.update(original_config)

    def test_recording_path_does_not_expand_refinement_error_sentinel(self):
        service = DictionarySnippetService(self.repository)
        service.replace(DictionarySnippets(snippets=(Snippet("Error", "success"),)))
        original_config = app.APP_CONFIG.copy()
        try:
            app.APP_CONFIG.update({
                "transcription_provider": "openai",
                "openai_api_key": "test-key",
                "openai_base_url": "https://proxy.example/v1",
                "openai_audio_model": "whisper-1",
                "refinement_provider": "openai",
                "refinement_model": "gpt-4o-mini",
            })
            with patch.object(app, "DICTIONARY_SERVICE", service), \
                    patch.object(
                        app.PROVIDER_REGISTRY,
                        "transcribe",
                        return_value=type("Result", (), {"text": "raw"})(),
                    ), patch.object(
                        app, "_refine_transcript", return_value="[Error: failed]"
                    ):
                result = app._call_provider_audio(
                    "openai", Path(self.directory.name) / "audio.wav", "prompt")

            self.assertEqual(result, "[Error: failed]")
        finally:
            app.APP_CONFIG.clear()
            app.APP_CONFIG.update(original_config)


if __name__ == "__main__":
    unittest.main()
