import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            error = app.requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.original_config = app.APP_CONFIG.copy()
        self.audio = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        self.audio.write(b"RIFF-test-audio")
        self.audio.close()
        self.audio_path = Path(self.audio.name)

    def tearDown(self):
        app.APP_CONFIG.clear()
        app.APP_CONFIG.update(self.original_config)
        self.audio_path.unlink(missing_ok=True)

    def test_provider_url_accepts_root_or_versioned_base(self):
        self.assertEqual(
            app._provider_url("https://proxy.example", "v1", "audio/transcriptions"),
            "https://proxy.example/v1/audio/transcriptions",
        )

    @staticmethod
    def _single_instance_api(already_exists=False):
        return SimpleNamespace(
            WAIT_OBJECT_0=0,
            create_event=Mock(return_value=11),
            create_mutex=Mock(return_value=(22, already_exists)),
            set_event=Mock(),
            wait_for_event=Mock(side_effect=[0, 1]),
            close=Mock(),
        )

    @patch("app.IS_WIN", True)
    def test_first_instance_keeps_mutex_and_activation_event(self):
        api = self._single_instance_api()

        guard = app.SingleInstanceGuard.acquire(api)

        self.assertIsNotNone(guard)
        self.assertEqual(guard.event_handle, 11)
        self.assertEqual(guard.mutex_handle, 22)
        api.set_event.assert_not_called()
        api.close.assert_not_called()

    @patch("app.IS_WIN", True)
    def test_second_instance_signals_existing_and_exits(self):
        api = self._single_instance_api(already_exists=True)

        guard = app.SingleInstanceGuard.acquire(api)

        self.assertIsNone(guard)
        api.set_event.assert_called_once_with(11)
        self.assertEqual(api.close.call_args_list[0].args, (22,))
        self.assertEqual(api.close.call_args_list[1].args, (11,))

    @patch("app.threading.Thread")
    def test_primary_instance_listens_for_later_launches(self, thread):
        api = self._single_instance_api()
        callback = Mock()
        thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"]()
        guard = app.SingleInstanceGuard(api, 11, 22)

        guard.start_activation_listener(callback)

        callback.assert_called_once_with()
        thread.assert_called_once_with(target=thread.call_args.kwargs["target"], daemon=True)

    @patch("app.subprocess.run")
    @patch("app.IS_WIN", True)
    def test_stale_recorder_cleanup_targets_only_our_temp_wav(self, run):
        app.Recorder._stop_stale_windows_recorders()

        command = run.call_args.args[0]
        self.assertEqual(command[:4], [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertIn(str(app.AUDIO_PATH), command[4])
        self.assertIn("$_.Name -ieq 'sox.exe'", command[4])
        self.assertIn("$_.CommandLine", command[4])

    @patch("app.subprocess.run")
    @patch("app.IS_WIN", False)
    def test_stale_recorder_cleanup_is_windows_only(self, run):
        app.Recorder._stop_stale_windows_recorders()

        run.assert_not_called()

    def test_official_openai_audio_models_are_documented_set(self):
        models = app._fetch_provider_models(
            "openai", "", "https://api.openai.com/v1")
        self.assertEqual(models, [
            "whisper-1",
            "gpt-4o-mini-transcribe",
            "gpt-4o-transcribe",
            "gpt-4o-transcribe-diarize",
        ])

    def test_official_groq_audio_models_are_documented_set(self):
        models = app._fetch_provider_models(
            "groq", "", "https://api.groq.com/openai/v1")
        self.assertEqual(models, [
            "whisper-large-v3-turbo",
            "whisper-large-v3",
        ])

    @patch("app.requests.get")
    def test_proxy_models_are_filtered_to_audio_transcription(self, get):
        get.return_value = FakeResponse({"data": [
            {"id": "gpt-5.4"},
            {"id": "whisper-1"},
            {"id": "gpt-4o-transcribe"},
        ]})
        models = app._fetch_provider_models(
            "openai", "proxy-key", "https://proxy.example")
        self.assertEqual(models, ["gpt-4o-transcribe", "whisper-1"])
        self.assertEqual(get.call_args.args[0], "https://proxy.example/v1/models")

    def test_openai_compatible_catalog_uses_id_instead_of_display_name(self):
        payload = {"data": [
            {"id": "whisper-large-v3-turbo", "name": "Whisper Large V3 Turbo"},
            {"id": "llama-3.3-70b-versatile", "name": "Llama 3.3 70B Versatile"},
        ]}
        self.assertEqual(
            app._parse_audio_models("groq", payload),
            ["whisper-large-v3-turbo"],
        )
        self.assertEqual(
            app._parse_text_models("groq", payload),
            ["llama-3.3-70b-versatile"],
        )

    def test_legacy_audio_model_label_is_migrated_to_api_id(self):
        self.assertEqual(
            app._canonical_audio_model("groq", "Whisper Large V3 Turbo"),
            "whisper-large-v3-turbo",
        )

    @patch("app.requests.get")
    def test_gemini_models_require_generate_content_support(self, get):
        get.return_value = FakeResponse({"models": [
            {"name": "models/gemini-audio", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-embed", "supportedGenerationMethods": ["embedContent"]},
        ]})
        models = app._fetch_provider_models(
            "gemini", "gemini-key", "https://generativelanguage.googleapis.com/v1beta")
        self.assertEqual(models, ["gemini-audio"])
        self.assertEqual(
            get.call_args.args[0],
            "https://generativelanguage.googleapis.com/v1beta/models",
        )

    @patch("app.requests.get")
    def test_gemini_credentials_are_validated_without_generation(self, get):
        get.return_value = FakeResponse({"models": []})
        app._validate_provider_credentials(
            "gemini", "gemini-key",
            "https://generativelanguage.googleapis.com/v1beta")
        self.assertEqual(
            get.call_args.args[0],
            "https://generativelanguage.googleapis.com/v1beta/models",
        )
        self.assertEqual(get.call_args.kwargs["headers"], {
            "x-goog-api-key": "gemini-key",
        })

    @patch("app.requests.get")
    def test_openai_compatible_credentials_use_models_endpoint(self, get):
        get.return_value = FakeResponse({"data": []})
        app._validate_provider_credentials(
            "groq", "groq-key", "https://api.groq.com/openai/v1")
        self.assertEqual(
            get.call_args.args[0], "https://api.groq.com/openai/v1/models")
        self.assertEqual(get.call_args.kwargs["headers"], {
            "Authorization": "Bearer groq-key",
        })

    def test_text_model_catalog_excludes_asr_and_non_llm_models(self):
        models = app._parse_text_models("openai", {"data": [
            {"id": "gpt-5.4-mini"},
            {"id": "whisper-1"},
            {"id": "gpt-4o-transcribe"},
            {"id": "text-embedding-3-small"},
            {"id": "gpt-image-1"},
            {"id": "gpt-4o-realtime-preview"},
        ]})
        self.assertEqual(models, ["gpt-5.4-mini"])
        self.assertEqual(
            app._provider_url("https://proxy.example/v1/", "v1", "audio/transcriptions"),
            "https://proxy.example/v1/audio/transcriptions",
        )

    @patch("app.requests.post")
    def test_gemini_official_endpoint_and_auth(self, post):
        app.APP_CONFIG.update({
            "gemini_api_key": "gemini-key",
            "gemini_base_url": "https://generativelanguage.googleapis.com/v1beta",
            "gemini_model": "gemini-2.5-flash",
        })
        post.return_value = FakeResponse({
            "candidates": [{"content": {"parts": [{"text": "hello"}]}}]
        })

        self.assertEqual(app.call_gemini(self.audio_path, "transcription"), "hello")
        args, kwargs = post.call_args
        self.assertEqual(
            args[0],
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash:generateContent",
        )
        self.assertEqual(kwargs["headers"], {"x-goog-api-key": "gemini-key"})

    @patch("app.requests.post")
    def test_gemini_custom_proxy_uses_bearer_and_v1beta(self, post):
        app.APP_CONFIG.update({
            "gemini_api_key": "proxy-key",
            "gemini_base_url": "https://proxy.example",
            "gemini_model": "gemini-3-flash",
        })
        post.return_value = FakeResponse({
            "candidates": [{"content": {"parts": [{"text": "proxy text"}]}}]
        })

        self.assertEqual(app.call_gemini(self.audio_path, "transcription"), "proxy text")
        args, kwargs = post.call_args
        self.assertEqual(
            args[0], "https://proxy.example/v1beta/models/gemini-3-flash:generateContent")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer proxy-key")

    @patch("app.requests.post")
    def test_openai_whisper_uses_multipart_proxy_endpoint(self, post):
        app.APP_CONFIG.update({
            "openai_api_key": "openai-key",
            "openai_base_url": "https://proxy.example",
            "openai_audio_model": "gpt-4o-transcribe",
        })
        post.return_value = FakeResponse({"text": "raw transcript"})

        result = app.call_openai(self.audio_path, "transcription", "en")

        self.assertEqual(result, "raw transcript")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://proxy.example/v1/audio/transcriptions")
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer openai-key"})
        self.assertEqual(kwargs["data"]["model"], "gpt-4o-transcribe")
        self.assertIn("file", kwargs["files"])

    @patch("app.requests.post")
    def test_openai_prompt_mode_rewrites_the_whisper_transcript(self, post):
        app.APP_CONFIG.update({
            "openai_api_key": "openai-key",
            "openai_base_url": "https://api.openai.com/v1",
            "openai_text_model": "gpt-4o-mini",
        })
        post.side_effect = [
            FakeResponse({"text": "rough transcript"}),
            FakeResponse({"choices": [{"message": {"content": "clear prompt"}}]}),
        ]

        result = app.call_openai(self.audio_path, "prompt", "en")

        self.assertEqual(result, "clear prompt")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            post.call_args_list[1].args[0], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(
            post.call_args_list[1].kwargs["json"]["model"], "gpt-4o-mini")

    @patch("app.requests.post")
    def test_groq_uses_official_audio_endpoint_and_configured_model(self, post):
        app.APP_CONFIG.update({
            "groq_api_key": "groq-key",
            "groq_base_url": "https://api.groq.com/openai/v1",
            "groq_audio_model": "whisper-large-v3-turbo",
        })
        post.return_value = FakeResponse({"text": "groq transcript"})

        result = app.call_groq(self.audio_path, "transcription", "pt")

        self.assertEqual(result, "groq transcript")
        args, kwargs = post.call_args
        self.assertEqual(
            args[0], "https://api.groq.com/openai/v1/audio/transcriptions")
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer groq-key"})
        self.assertEqual(kwargs["data"]["model"], "whisper-large-v3-turbo")
        self.assertEqual(kwargs["data"]["language"], "pt")

    @patch("app.requests.post")
    def test_groq_request_defensively_canonicalizes_legacy_model_label(self, post):
        app.APP_CONFIG.update({
            "groq_api_key": "groq-key",
            "groq_base_url": "https://api.groq.com/openai/v1",
            "groq_audio_model": "Whisper Large V3 Turbo",
        })
        post.return_value = FakeResponse({"text": "transcript"})

        self.assertEqual(
            app.call_groq(self.audio_path, "transcription", "en"), "transcript")
        self.assertEqual(
            post.call_args.kwargs["data"]["model"], "whisper-large-v3-turbo")

    @patch("app.requests.post")
    def test_groq_asr_can_use_openai_for_text_refinement(self, post):
        app.APP_CONFIG.update({
            "groq_api_key": "groq-key",
            "groq_base_url": "https://api.groq.com/openai/v1",
            "groq_audio_model": "whisper-large-v3-turbo",
            "openai_api_key": "openai-key",
            "openai_base_url": "https://api.openai.com/v1",
            "refinement_provider": "openai",
            "refinement_model": "gpt-5.4-mini",
        })
        post.side_effect = [
            FakeResponse({"text": "rough transcript"}),
            FakeResponse({"choices": [{"message": {"content": "refined text"}}]}),
        ]

        result = app.call_groq(self.audio_path, "prompt", "en")

        self.assertEqual(result, "refined text")
        self.assertEqual(
            post.call_args_list[1].args[0], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(
            post.call_args_list[1].kwargs["json"]["model"], "gpt-5.4-mini")

    @patch("app.call_openai", return_value="openai result")
    def test_selected_provider_is_used_automatically(self, openai):
        app.APP_CONFIG["transcription_provider"] = "openai"
        self.assertEqual(
            app.call_transcription_provider(self.audio_path, "transcription"),
            "openai result",
        )
        openai.assert_called_once()

    @patch("app.call_groq", return_value="groq result")
    def test_selected_groq_provider_is_used_automatically(self, groq):
        app.APP_CONFIG["transcription_provider"] = "groq"
        self.assertEqual(
            app.call_transcription_provider(self.audio_path, "transcription"),
            "groq result",
        )
        groq.assert_called_once()

    @patch("app.requests.post")
    def test_selected_text_rewrite_preserves_language_with_openai(self, post):
        app.APP_CONFIG.update({
            "refinement_provider": "openai",
            "refinement_model": "gpt-4o-mini",
            "openai_api_key": "openai-key",
            "openai_base_url": "https://api.openai.com/v1",
        })
        post.return_value = FakeResponse({
            "choices": [{"message": {"content": "Texto melhorado."}}]
        })

        self.assertEqual(
            app.rewrite_selected_text("texto mal organizado"), "Texto melhorado.")
        request = post.call_args.kwargs["json"]
        self.assertEqual(request["model"], "gpt-4o-mini")
        instruction = request["messages"][0]["content"]
        self.assertIn("Preserve the original language", instruction)
        self.assertNotIn("Output MUST be", instruction)

    @patch("app.requests.post")
    def test_selected_text_rewrite_uses_gemini_text_endpoint(self, post):
        app.APP_CONFIG.update({
            "refinement_provider": "gemini",
            "refinement_model": "gemini-2.5-flash",
            "gemini_api_key": "gemini-key",
            "gemini_base_url": "https://generativelanguage.googleapis.com/v1beta",
        })
        post.return_value = FakeResponse({
            "candidates": [{"content": {"parts": [{"text": "Clear text."}]}}]
        })

        self.assertEqual(app.rewrite_selected_text("unclear text"), "Clear text.")
        self.assertEqual(
            post.call_args.args[0],
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash:generateContent",
        )
        instruction = post.call_args.kwargs["json"]["systemInstruction"]["parts"][0]["text"]
        self.assertIn("Preserve the original language", instruction)

    @patch("app.requests.post")
    def test_selected_text_rewrite_routes_to_groq(self, post):
        app.APP_CONFIG.update({
            "refinement_provider": "groq",
            "refinement_model": "llama-3.3-70b-versatile",
            "groq_api_key": "groq-key",
            "groq_base_url": "https://api.groq.com/openai/v1",
        })
        post.return_value = FakeResponse({
            "choices": [{"message": {"content": "Rewritten."}}]
        })

        self.assertEqual(app.rewrite_selected_text("rewrite me"), "Rewritten.")
        self.assertEqual(
            post.call_args.args[0], "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(
            post.call_args.kwargs["json"]["model"], "llama-3.3-70b-versatile")

    def test_selected_text_rewrite_rejects_empty_input_and_empty_response(self):
        self.assertTrue(app.rewrite_selected_text("  ").startswith("[Error"))
        app.APP_CONFIG.update({
            "refinement_provider": "openai",
            "refinement_model": "gpt-4o-mini",
            "openai_api_key": "openai-key",
        })
        with patch("app._rewrite_openai_compatible", return_value=""):
            self.assertTrue(app.rewrite_selected_text("source").startswith("[Error"))

    def test_selection_prompt_expands_unambiguous_chat_abbreviations(self):
        instruction = app.SELECTION_REWRITE_INSTRUCTION
        self.assertIn(app.FAITHFUL_REWRITE_INSTRUCTION, instruction)
        self.assertIn("editing, not summarization", instruction)
        self.assertIn("preserve every requirement", instruction)
        self.assertIn("'vc' to 'você'", instruction)
        self.assertIn("Oi, tudo bem com você?", instruction)
        self.assertIn("Never return the input unchanged", instruction)

    def test_prompt_mode_requires_faithful_editing_instead_of_summary(self):
        instruction = app.PROMPT_INSTRUCTION
        self.assertIn("editing, not summarization", instruction)
        self.assertIn("preserve every requirement", instruction)
        self.assertIn("technical identifier", instruction)
        self.assertIn("imperative wording", instruction)
        self.assertIn("Preserve attention directives", instruction)
        self.assertIn("API keys distinct from routing choices", instruction)
        self.assertIn("custom base URL/proxy", instruction)
        self.assertIn("proxy eliminates authentication", instruction)
        self.assertIn("smallest structural edits", instruction)
        self.assertNotIn("Write in the first person", instruction)

    @patch("app.requests.post")
    def test_gemini_prompt_uses_low_temperature_for_faithful_editing(self, post):
        app.APP_CONFIG.update({
            "gemini_api_key": "gemini-key",
            "gemini_base_url": "https://generativelanguage.googleapis.com/v1beta",
            "gemini_model": "gemini-2.5-flash",
        })
        post.return_value = FakeResponse({
            "candidates": [{"content": {"parts": [{"text": "Edited text"}]}}]
        })

        self.assertEqual(app.call_gemini(self.audio_path, "prompt"), "Edited text")
        request = post.call_args.kwargs["json"]
        self.assertEqual(request["generationConfig"]["temperature"], 0.1)

    def test_refinement_selection_persists_when_gemini_transcribes(self):
        app._apply_selected_models(
            {"provider": "gemini", "model": "gemini-2.5-flash"},
            {"provider": "openai", "model": "gpt-4o-mini"},
            [("gemini", "gemini-2.5-flash")],
            [("openai", "gpt-4o-mini")],
            {"gemini": "gemini_model", "openai": "openai_audio_model"},
        )
        self.assertEqual(app.APP_CONFIG["transcription_provider"], "gemini")
        self.assertEqual(app.APP_CONFIG["refinement_provider"], "openai")
        self.assertEqual(app.APP_CONFIG["refinement_model"], "gpt-4o-mini")

    def test_ui_mode_and_language_are_loaded_from_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({
                "ui_mode": "transcription",
                "ui_language": "pt",
            }), encoding="utf-8")
            with patch("app.CONFIG_PATH", config_path):
                config = app._load_app_config()

        self.assertEqual(config["ui_mode"], "transcription")
        self.assertEqual(config["ui_language"], "pt")

    @patch("app._save_app_config")
    def test_ui_preferences_are_saved_immediately(self, save):
        harness = SimpleNamespace(mode="transcription", lang="pt")

        app.App._save_ui_preferences(harness)

        self.assertEqual(app.APP_CONFIG["ui_mode"], "transcription")
        self.assertEqual(app.APP_CONFIG["ui_language"], "pt")
        save.assert_called_once_with()

    @patch("app.IS_WIN", True)
    def test_autostart_registry_entry_launches_hidden(self):
        class Key:
            def __init__(self, registry):
                self.registry = registry

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Registry:
            HKEY_CURRENT_USER = 1
            REG_SZ = 1

            def __init__(self):
                self.values = {}

            def CreateKey(self, *_args):
                return Key(self)

            def OpenKey(self, *_args):
                return Key(self)

            def SetValueEx(self, _key, name, _reserved, _kind, value):
                self.values[name] = value

            def QueryValueEx(self, _key, name):
                if name not in self.values:
                    raise FileNotFoundError(name)
                return self.values[name], self.REG_SZ

            def DeleteValue(self, _key, name):
                if name not in self.values:
                    raise FileNotFoundError(name)
                del self.values[name]

        registry = Registry()
        with patch.object(app.sys, "frozen", True, create=True), patch.object(
                app.sys, "executable", r"C:\Apps\ClarifyVoice.exe"):
            app._set_autostart(True, registry)

        self.assertTrue(app._is_autostart_enabled(registry))
        self.assertIn(r"C:\Apps\ClarifyVoice.exe", registry.values["ClarifyVoice"])
        self.assertTrue(registry.values["ClarifyVoice"].endswith("--hidden"))

        app._set_autostart(False, registry)
        self.assertFalse(app._is_autostart_enabled(registry))

    def test_models_and_settings_labels_follow_interface_language(self):
        self.assertEqual(app.STRINGS["en"]["models_section"], "Models")
        self.assertEqual(app.STRINGS["pt"]["models_section"], "Modelos")
        self.assertEqual(app.STRINGS["en"]["settings_section"], "Settings")
        self.assertEqual(app.STRINGS["pt"]["settings_section"], "Configurações")


class RewriteWorkflowTests(unittest.TestCase):
    class Harness:
        def __init__(self):
            self.finished = []

        def after(self, _delay, callback):
            callback()

        def _finish_rewrite(self, text=None, status_key=None):
            self.finished.append((text, status_key))

        _restore_clipboard_text = staticmethod(app.App._restore_clipboard_text)

    @patch("app.keyboard", new=SimpleNamespace(is_pressed=lambda _key: False))
    @patch("app.time.sleep")
    @patch("app._send_key_chord")
    @patch("app._set_windows_clipboard_text")
    @patch("app.rewrite_selected_text", return_value="Texto revisado.")
    @patch("app._copy_selected_text", side_effect=["Texto original.", "Texto original."])
    @patch("app._get_windows_clipboard_text", return_value="clipboard anterior")
    @patch("app._foreground_window_handle", side_effect=[77, 77])
    @patch("app._record_usage_event")
    def test_safe_selection_is_pasted_once(self, record_usage, _foreground, _clipboard, _copy,
            _rewrite, set_clipboard, send_key, _sleep):
        harness = self.Harness()
        app.App._rewrite_selection_worker(harness, 77)

        set_clipboard.assert_called_once_with("Texto revisado.")
        send_key.assert_called_once_with("ctrl+v")
        record_usage.assert_called_once()
        self.assertEqual(harness.finished, [("Texto revisado.", None)])

    @patch("app.keyboard", new=SimpleNamespace(is_pressed=lambda _key: False))
    @patch("app._send_key_chord")
    @patch("app._set_windows_clipboard_text")
    @patch("app.rewrite_selected_text", return_value="Rewritten")
    @patch("app._copy_selected_text", return_value="Original")
    @patch("app._get_windows_clipboard_text", return_value="previous")
    @patch("app._foreground_window_handle", return_value=88)
    @patch("app._record_usage_event")
    def test_focus_change_copies_without_pasting(self, record_usage, _foreground, _clipboard,
            _copy, _rewrite, set_clipboard, send_key):
        harness = self.Harness()
        app.App._rewrite_selection_worker(harness, 77)

        set_clipboard.assert_called_once_with("Rewritten")
        send_key.assert_not_called()
        record_usage.assert_called_once()
        self.assertEqual(harness.finished, [("Rewritten", "rewrite_copied")])

    @patch("app.keyboard", new=SimpleNamespace(is_pressed=lambda _key: False))
    @patch("app._send_key_chord")
    @patch("app._set_windows_clipboard_text")
    @patch("app.rewrite_selected_text", return_value="Rewritten")
    @patch("app._copy_selected_text", side_effect=["Original", "Other selection"])
    @patch("app._get_windows_clipboard_text", return_value="previous")
    @patch("app._foreground_window_handle", return_value=77)
    @patch("app._record_usage_event")
    def test_selection_change_copies_without_pasting(self, record_usage, _foreground,
            _clipboard, _copy, _rewrite, set_clipboard, send_key):
        harness = self.Harness()
        app.App._rewrite_selection_worker(harness, 77)

        set_clipboard.assert_called_once_with("Rewritten")
        send_key.assert_not_called()
        record_usage.assert_called_once()
        self.assertEqual(harness.finished, [("Rewritten", "rewrite_copied")])

    @patch("app.keyboard", new=SimpleNamespace(is_pressed=lambda _key: False))
    @patch("app._set_windows_clipboard_text")
    @patch("app.rewrite_selected_text", return_value="[Error: failed]")
    @patch("app._copy_selected_text", return_value="Original")
    @patch("app._get_windows_clipboard_text", return_value="previous")
    def test_provider_failure_restores_text_clipboard(self, _clipboard, _copy,
            _rewrite, set_clipboard):
        harness = self.Harness()
        app.App._rewrite_selection_worker(harness, 77)

        set_clipboard.assert_called_once_with("previous")
        self.assertEqual(harness.finished, [(None, "rewrite_failed")])

    @patch("app.keyboard", new=SimpleNamespace(is_pressed=lambda _key: False))
    @patch("app._set_windows_clipboard_text")
    @patch("app._copy_selected_text", return_value="  \r\n")
    @patch("app._get_windows_clipboard_text", return_value="previous")
    def test_empty_selection_skips_ai_and_restores_clipboard(self, _clipboard,
            _copy, set_clipboard):
        harness = self.Harness()
        with patch("app.rewrite_selected_text") as rewrite:
            app.App._rewrite_selection_worker(harness, 77)
        rewrite.assert_not_called()
        set_clipboard.assert_called_once_with("previous")
        self.assertEqual(harness.finished, [(None, "no_selection")])

    def test_selection_comparison_normalizes_windows_newlines(self):
        self.assertTrue(app._same_selected_text("a\r\nb", "a\nb"))

    def test_single_line_result_uses_compact_textbox_height(self):
        self.assertEqual(app._estimate_result_lines("Oi, tudo bem?"), 1)
        self.assertEqual(app._result_text_height("Oi, tudo bem?"), 38)

    def test_result_height_grows_with_content_and_is_bounded(self):
        self.assertGreater(app._result_text_height("linha\n" * 6), 38)
        self.assertEqual(app._result_text_height("texto", display_lines=100), 220)

    def test_result_window_height_uses_content_instead_of_frame_default(self):
        self.assertEqual(app._result_window_height(26, 80), 130)
        self.assertEqual(app._result_window_height(26, 500), 360)

    @patch("app.time.sleep")
    @patch("app._get_windows_clipboard_text", return_value="selected")
    @patch("app._clipboard_sequence_number", side_effect=[10, 10, 11])
    @patch("app._send_key_chord")
    def test_copy_selection_uses_bounded_on_demand_polling(self, send_key,
            _sequence, _clipboard, _sleep):
        self.assertEqual(app._copy_selected_text(timeout=0.1), "selected")
        send_key.assert_called_once_with("ctrl+c")

    @patch("app.IS_WIN", True)
    @patch("app._foreground_window_handle", return_value=77)
    @patch("app.threading.Thread")
    def test_repeated_hotkey_does_not_queue_another_worker(self, thread,
            _foreground):
        harness = SimpleNamespace(
            app_state="ready",
            _rewrite_active=False,
            _begin_rewrite_feedback=lambda: None,
            _rewrite_selection_worker=lambda _target: None,
            after=lambda _delay, callback: callback(),
        )

        app.App._rewrite_hotkey(harness)
        app.App._rewrite_hotkey(harness)

        self.assertTrue(harness._rewrite_active)
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()

    def test_successful_rewrite_keeps_feedback_until_check_finishes(self):
        deferred = []
        ready_callbacks = []
        states = []
        results = []
        harness = SimpleNamespace(
            _rewrite_active=True,
            _show_success_then=lambda callback: deferred.append(callback),
            _set_state=lambda state, text="", after_ready=None: (
                states.append((state, text)),
                ready_callbacks.append(after_ready)),
            _show_result=results.append,
            _t=lambda key: key,
        )

        app.App._finish_rewrite(harness, text="Texto revisado.")

        self.assertTrue(harness._rewrite_active)
        self.assertEqual(states, [])
        self.assertEqual(results, [])
        self.assertEqual(len(deferred), 1)

        deferred[0]()
        self.assertTrue(harness._rewrite_active)
        self.assertEqual(states, [("ready", "")])
        self.assertEqual(results, [])

        ready_callbacks[0]()
        self.assertFalse(harness._rewrite_active)
        self.assertEqual(results, ["Texto revisado."])

    @patch("app.threading.Thread")
    def test_recording_result_waits_for_success_check_before_restoring_ui(self,
            thread):
        deferred = []
        ready_callbacks = []
        states = []
        results = []
        harness = SimpleNamespace(
            _show_success_then=lambda callback: deferred.append(callback),
            _set_state=lambda state, after_ready=None: (
                states.append(state), ready_callbacks.append(after_ready)),
            _show_result=results.append,
        )

        app.App._on_result(harness, "Transcrição pronta.")

        thread.assert_called_once()
        thread.return_value.start.assert_called_once()
        self.assertEqual(states, [])
        self.assertEqual(results, [])

        deferred[0]()
        self.assertEqual(states, ["ready"])
        self.assertEqual(results, [])

        ready_callbacks[0]()
        self.assertEqual(results, ["Transcrição pronta."])

    @patch("app.time.perf_counter", return_value=10.14)
    def test_pill_fade_out_finishes_after_reaching_zero_opacity(self, _clock):
        opacities = []
        finished = []
        harness = SimpleNamespace(
            _wave_running=True,
            app_state="dismissing",
            _pill_transition_started=10.0,
            _set_pill_opacity=opacities.append,
            _finish_pill_dismissal=lambda: finished.append(True),
        )

        app.App._wave_tick(harness)

        self.assertEqual(opacities, [0.0])
        self.assertEqual(finished, [True])

    @patch("app.time.perf_counter", return_value=12.0)
    def test_ready_state_starts_fade_before_destroying_pill(self, _clock):
        callback = Mock()
        harness = SimpleNamespace(
            app_state="processing",
            _wave_running=True,
            _timer_running=True,
            _pill_pending_ready=None,
            _pill_transition_started=0.0,
        )

        app.App._set_state(
            harness, "ready", "Pronto", after_ready=callback)

        self.assertEqual(harness.app_state, "dismissing")
        self.assertFalse(harness._timer_running)
        self.assertEqual(harness._pill_transition_started, 12.0)
        self.assertEqual(harness._pill_pending_ready, ("Pronto", callback))
        callback.assert_not_called()

    def test_second_launch_reveals_hidden_app(self):
        show = Mock()
        harness = SimpleNamespace(
            _recording_overlay=None,
            winfo_viewable=lambda: False,
            _show_without_activation=show,
        )

        app.App._show_if_hidden(harness)

        show.assert_called_once_with()

    def test_second_launch_does_nothing_when_app_is_visible(self):
        show = Mock()
        harness = SimpleNamespace(
            _recording_overlay=None,
            winfo_viewable=lambda: True,
            _show_without_activation=show,
        )

        app.App._show_if_hidden(harness)

        show.assert_not_called()


class WindowFadeTests(unittest.TestCase):
    class Widget:
        def __init__(self, opacity=1.0):
            self.opacity = opacity
            self.opacity_history = []
            self._clarify_fade_job = None
            self._clarify_fading_out = False
            self._main_backdrop = SimpleNamespace(set_opacity=Mock())

        def attributes(self, name, value=None):
            self.assert_alpha_name(name)
            if value is None:
                return self.opacity
            self.opacity = value
            self.opacity_history.append(value)

        @staticmethod
        def assert_alpha_name(name):
            if name != "-alpha":
                raise AssertionError(name)

        def winfo_exists(self):
            return True

        def after(self, _delay, callback):
            callback()
            return "job"

        def after_cancel(self, _job):
            return None

        def deiconify(self):
            return None

    @patch("app.time.perf_counter", side_effect=[0.0, 0.15])
    def test_fade_in_updates_window_and_native_backdrop(self, _clock):
        widget = self.Widget()
        shown = Mock()

        app._fade_in_window(widget, shown)

        shown.assert_called_once_with()
        self.assertEqual(widget.opacity_history, [0.0, 1.0])
        self.assertEqual(
            widget._main_backdrop.set_opacity.call_args_list,
            [unittest.mock.call(0.0), unittest.mock.call(1.0)])

    @patch("app.time.perf_counter", side_effect=[0.0, 0.14])
    def test_fade_out_completes_before_window_is_reset(self, _clock):
        widget = self.Widget()
        completed = Mock()

        app._fade_out_window(widget, completed)

        completed.assert_called_once_with()
        self.assertEqual(widget.opacity_history, [0.0, 1.0])
        self.assertFalse(widget._clarify_fading_out)

    def test_window_header_drag_updates_window_position(self):
        bindings = {}
        handle = SimpleNamespace(bind=lambda event, callback, add=None:
            bindings.__setitem__(event, callback))
        widget = SimpleNamespace(
            winfo_x=lambda: 100,
            winfo_y=lambda: 50,
            geometry=Mock(),
        )
        app._make_window_draggable(widget, handle)

        bindings["<Button-1>"](SimpleNamespace(x_root=130, y_root=80))
        bindings["<B1-Motion>"](SimpleNamespace(x_root=230, y_root=180))

        widget.geometry.assert_called_once_with("+200+150")


class UsageStatisticsTests(unittest.TestCase):
    def test_recording_event_tracks_models_cost_and_no_transcript(self):
        event = app._build_recording_usage_event({
            "provider": "openai",
            "model": "whisper-1",
            "mode": "prompt",
            "refinement_provider": "openai",
            "refinement_model": "gpt-4o-mini",
        }, 60, "A short polished transcript")

        self.assertEqual(event["type"], "recording")
        self.assertEqual(event["word_count"], 4)
        self.assertEqual([entry["model"] for entry in event["models"]], [
            "whisper-1", "gpt-4o-mini"])
        self.assertGreaterEqual(event["estimated_cost_usd"], 0.006)
        self.assertNotIn("text", event)
        self.assertNotIn("transcript", event)

    def test_summary_ranks_models_and_aggregates_recording_metrics(self):
        now = 2_000_000.0
        events = [
            {"timestamp": now, "type": "recording", "duration_seconds": 30,
             "word_count": 50, "estimated_cost_usd": 0.01, "cost_complete": True,
             "models": [{"provider": "groq", "model": "whisper-large-v3-turbo"}]},
            {"timestamp": now, "type": "recording", "duration_seconds": 90,
             "word_count": 120, "estimated_cost_usd": 0.02, "cost_complete": True,
             "models": [{"provider": "groq", "model": "whisper-large-v3-turbo"}]},
            {"timestamp": now, "type": "rewrite", "duration_seconds": 0,
             "word_count": 10, "estimated_cost_usd": 0.001, "cost_complete": True,
             "models": [{"provider": "openai", "model": "gpt-4o-mini"}]},
        ]

        summary = app._usage_summary(events, now=now)

        self.assertEqual(summary["recordings"], 2)
        self.assertEqual(summary["rewrites"], 1)
        self.assertEqual(summary["total_seconds"], 120)
        self.assertEqual(summary["average_seconds"], 60)
        self.assertEqual(summary["total_words"], 170)
        self.assertEqual(summary["ranked_models"][0],
            (("groq", "whisper-large-v3-turbo"), 2))

    def test_usage_events_are_persisted_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            stats_path = Path(directory) / "usage_stats.json"
            with patch.object(app, "STATS_PATH", stats_path):
                app._record_usage_event({"type": "recording", "models": []})
                app._record_usage_event({"type": "rewrite", "models": []})
                events = app._load_usage_events()

        self.assertEqual([event["type"] for event in events], [
            "recording", "rewrite"])

    def test_duration_formatter_is_compact(self):
        self.assertEqual(app._format_duration(65), "1m 05s")
        self.assertEqual(app._format_duration(3661), "1h 01m")


if __name__ == "__main__":
    unittest.main()
