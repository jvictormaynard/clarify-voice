"""Deterministic contracts for the voice-translation foundation (#49)."""

from __future__ import annotations

import threading
import unittest

from provider_types import TranslationResult, TranscriptionResult
from voice_translation import (
    AUTO_LANGUAGE,
    PublicationDecision,
    VoiceTranslationBusyError,
    VoiceTranslationConfig,
    VoiceTranslationConfigurationError,
    VoiceTranslationLanguages,
    VoiceTranslationPhase,
    VoiceTranslationPublication,
    VoiceTranslationPublicationCoordinator,
    VoiceTranslationPublicationPolicy,
    VoiceTranslationRoute,
    VoiceTranslationStateMachine,
    VoiceTranslationWorkflow,
    normalize_language_tag,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeProvider:
    def __init__(self) -> None:
        self.raw = " Olá do microfone "
        self.translated = "Hello from the microphone"
        self.translation_error: Exception | None = None
        self.translation_empty = False
        self.transcribe_calls: list[tuple[object, str]] = []
        self.translation_requests = []

    def transcribe(self, audio_source, source_language):
        self.transcribe_calls.append((audio_source, source_language))
        return TranscriptionResult(self.raw, "gemini", "gemini-test")

    def translate(self, request):
        self.translation_requests.append(request)
        if self.translation_error is not None:
            raise self.translation_error
        if self.translation_empty:
            return TranslationResult("", "openai", "gpt-test", request.target_language)
        return TranslationResult(
            self.translated,
            request.route.provider_id,
            request.route.model_id,
            request.target_language,
        )


class FakeClipboard:
    def __init__(self) -> None:
        self.target = "editor-window"
        self.target_current = True
        self.clipboard_owned = True
        self.published = []

    def capture_target(self):
        return self.target

    def is_target_current(self, target):
        return self.target_current and target == self.target

    def owns_clipboard(self):
        return self.clipboard_owned

    def publish(self, text, target, disposition):
        self.published.append((text, target, disposition))


class BlockingClipboard(FakeClipboard):
    def __init__(self) -> None:
        super().__init__()
        self.publish_entered = threading.Event()
        self.publish_release = threading.Event()

    def publish(self, text, target, disposition):
        self.publish_entered.set()
        if not self.publish_release.wait(timeout=2):
            raise AssertionError("test did not release clipboard publication")
        super().publish(text, target, disposition)


class InterleavingStateMachine(VoiceTranslationStateMachine):
    """Pause just before the transition to expose phase/cancel races."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.transition_entered = threading.Event()
        self.transition_release = threading.Event()
        self.cancel_entered = threading.Event()
        self.block_translation = False

    def _set(self, **changes):
        if (
            self.block_translation
            and changes.get("phase") is VoiceTranslationPhase.TRANSLATING
        ):
            self.transition_entered.set()
            if not self.transition_release.wait(timeout=2):
                raise AssertionError("test did not release state transition")
            self.block_translation = False
        return super()._set(**changes)

    def cancel(self, *args, **kwargs):
        self.cancel_entered.set()
        return super().cancel(*args, **kwargs)


class VoiceTranslationConfigTests(unittest.TestCase):
    def test_language_tags_are_normalized_and_target_cannot_be_auto(self):
        self.assertEqual(normalize_language_tag("pt_br"), "pt-BR")
        self.assertEqual(normalize_language_tag("zh-hant-tw"), "zh-Hant-TW")
        self.assertEqual(
            VoiceTranslationLanguages("AUTO", "de_de").source_language,
            AUTO_LANGUAGE,
        )
        with self.assertRaises(VoiceTranslationConfigurationError):
            VoiceTranslationLanguages(AUTO_LANGUAGE, AUTO_LANGUAGE)

    def test_versioned_config_round_trips_flat_language_fields_and_route(self):
        config = VoiceTranslationConfig.from_mapping({
            "schema_version": 1,
            "source_language": "pt-BR",
            "target_language": "en-us",
            "route": {
                "provider_id": "GROQ",
                "model_id": "llama-3.3-70b-versatile",
                "prompt": "Translate only; preserve names.",
            },
        })

        self.assertEqual(config.source_language, "pt-BR")
        self.assertEqual(config.target_language, "en-US")
        self.assertEqual(config.route.provider_id, "groq")
        self.assertEqual(
            VoiceTranslationConfig.from_mapping(config.to_mapping()), config
        )

    def test_route_validation_uses_text_generation_capability(self):
        validated = VoiceTranslationConfig(
            route=VoiceTranslationRoute(
                provider_id="groq",
                model_id="",
                prompt="",
            )
        ).validate()
        self.assertEqual(validated.route.model_id, "llama-3.3-70b-versatile")
        self.assertTrue(validated.route.prompt)
        with self.assertRaises(VoiceTranslationConfigurationError) as context:
            VoiceTranslationConfig(
                route=VoiceTranslationRoute(provider_id="local_asr")
            ).validate()
        self.assertIn("text generation", str(context.exception).lower())

    def test_diagnostics_omit_prompt_and_url_credentials(self):
        config = VoiceTranslationConfig(
            route=VoiceTranslationRoute(
                provider_id="openai",
                prompt="never expose this instruction",
                custom_endpoint="https://proxy.example/v1?token=secret",
            )
        )
        diagnostics = config.diagnostic_mapping()
        self.assertNotIn("never expose", str(diagnostics))
        self.assertNotIn("secret", str(diagnostics))
        self.assertEqual(
            diagnostics["route"]["custom_endpoint"], "https://proxy.example/v1"
        )

    def test_future_schema_is_rejected_before_route_validation(self):
        with self.assertRaises(VoiceTranslationConfigurationError):
            VoiceTranslationConfig.from_mapping({"schema_version": 2})


class VoiceTranslationPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = VoiceTranslationPublicationPolicy()

    def test_translation_failure_returns_raw_copy_only(self):
        decision = self.policy.decide(
            raw_transcript="raw transcript",
            translated_text="",
            target_current=True,
            clipboard_owned=True,
            translation_available=False,
        )
        self.assertEqual(decision.disposition, VoiceTranslationPublication.COPY_ONLY)
        self.assertEqual(decision.text, "raw transcript")
        self.assertEqual(decision.reason, "translation_unavailable_raw_transcript")

    def test_focus_or_clipboard_loss_never_pastes(self):
        for target_current, clipboard_owned, reason in (
            (False, True, "target_not_current"),
            (True, False, "clipboard_not_owned"),
        ):
            with self.subTest(reason=reason):
                decision = self.policy.decide(
                    raw_transcript="raw",
                    translated_text="translated",
                    target_current=target_current,
                    clipboard_owned=clipboard_owned,
                )
                self.assertEqual(
                    decision.disposition, VoiceTranslationPublication.COPY_ONLY
                )
                self.assertEqual(decision.reason, reason)

    def test_safe_target_and_clipboard_can_paste(self):
        decision = self.policy.decide(
            raw_transcript="raw",
            translated_text="translated",
            target_current=True,
            clipboard_owned=True,
        )
        self.assertEqual(decision, PublicationDecision(
            VoiceTranslationPublication.PASTED,
            "translated",
            "target_and_clipboard_safe",
        ))


class VoiceTranslationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = FakeProvider()
        self.clipboard = FakeClipboard()
        self.clock = FakeClock()
        self.config = VoiceTranslationConfig(
            languages=VoiceTranslationLanguages("auto", "de-DE"),
            route=VoiceTranslationRoute(
                provider_id="openai",
                model_id="gpt-4o-mini",
                prompt="Translate voice only.",
            ),
        )

    def workflow(self) -> VoiceTranslationWorkflow:
        return VoiceTranslationWorkflow(
            self.provider,
            self.clipboard,
            self.config,
            clock=self.clock,
        )

    def test_success_passes_source_language_and_dedicated_route(self):
        workflow = self.workflow()
        state = workflow.run(b"audio")

        self.assertEqual(state.phase, VoiceTranslationPhase.COMPLETED)
        self.assertEqual(state.raw_transcript, "Olá do microfone")
        self.assertEqual(state.translated_text, "Hello from the microphone")
        self.assertEqual(state.publication, VoiceTranslationPublication.PASTED)
        self.assertEqual(self.provider.transcribe_calls, [(b"audio", "auto")])
        request = self.provider.translation_requests[0]
        self.assertEqual(request.target_language, "de-DE")
        self.assertEqual(request.route.prompt, "Translate voice only.")
        self.assertEqual(request.route.provider_id, "openai")
        self.assertEqual(
            self.clipboard.published,
            [("Hello from the microphone", "editor-window", VoiceTranslationPublication.PASTED)],
        )
        self.assertEqual(
            [snapshot.phase for snapshot in workflow.history],
            [
                VoiceTranslationPhase.READY,
                VoiceTranslationPhase.RECORDING,
                VoiceTranslationPhase.TRANSCRIBING,
                VoiceTranslationPhase.TRANSLATING,
                VoiceTranslationPhase.PUBLISHING,
                VoiceTranslationPhase.PUBLISHING,
                VoiceTranslationPhase.COMPLETED,
            ],
        )

    def test_translation_failure_copies_raw_transcript_and_marks_failure(self):
        self.provider.translation_error = RuntimeError("provider unavailable")
        state = self.workflow().run(b"audio")

        self.assertEqual(state.phase, VoiceTranslationPhase.FAILED)
        self.assertEqual(state.failure_code, "translation_failed")
        self.assertEqual(state.raw_transcript, "Olá do microfone")
        self.assertEqual(state.published_text, "Olá do microfone")
        self.assertEqual(state.publication, VoiceTranslationPublication.COPY_ONLY)
        self.assertEqual(
            self.clipboard.published,
            [("Olá do microfone", "editor-window", VoiceTranslationPublication.COPY_ONLY)],
        )

    def test_empty_translation_keeps_raw_transcript(self):
        self.provider.translation_empty = True
        state = self.workflow().run(b"audio")

        self.assertEqual(state.phase, VoiceTranslationPhase.FAILED)
        self.assertEqual(state.failure_code, "empty_translation")
        self.assertEqual(state.raw_transcript, "Olá do microfone")
        self.assertEqual(state.publication, VoiceTranslationPublication.COPY_ONLY)

    def test_focus_loss_downgrades_to_copy_only(self):
        self.clipboard.target_current = False
        state = self.workflow().run(b"audio")

        self.assertEqual(state.phase, VoiceTranslationPhase.COMPLETED)
        self.assertEqual(state.publication, VoiceTranslationPublication.COPY_ONLY)
        self.assertEqual(state.publication_reason, "target_not_current")
        self.assertEqual(self.clipboard.published[0][0], "Hello from the microphone")

    def test_empty_transcript_does_not_publish(self):
        self.provider.raw = "  "
        state = self.workflow().run(b"audio")

        self.assertEqual(state.phase, VoiceTranslationPhase.FAILED)
        self.assertEqual(state.failure_code, "empty_transcript")
        self.assertEqual(state.raw_transcript, "")
        self.assertEqual(self.clipboard.published, [])


class VoiceTranslationConcurrencyTests(unittest.TestCase):
    def test_publication_coordinator_rejects_overlap_until_release(self):
        coordinator = VoiceTranslationPublicationCoordinator()
        self.assertTrue(coordinator.claim(1))
        self.assertFalse(coordinator.claim(2))
        self.assertEqual(coordinator.active_operation_id, 1)
        self.assertFalse(coordinator.release(2))
        self.assertTrue(coordinator.release(1))
        self.assertTrue(coordinator.claim(2))

    def test_state_machine_rejects_new_operation_while_active(self):
        config = VoiceTranslationConfig()
        machine = VoiceTranslationStateMachine(config, clock=FakeClock())
        machine.begin()
        with self.assertRaises(VoiceTranslationBusyError):
            machine.begin()
        machine.cancel()
        self.assertEqual(machine.state.phase, VoiceTranslationPhase.CANCELLED)
        self.assertEqual(machine.begin().operation_id, 2)

    def test_default_workflows_share_global_publication_coordinator(self):
        provider = FakeProvider()
        first = VoiceTranslationWorkflow(
            provider, FakeClipboard(), VoiceTranslationConfig(), clock=FakeClock()
        )
        second = VoiceTranslationWorkflow(
            provider, FakeClipboard(), VoiceTranslationConfig(), clock=FakeClock()
        )
        self.assertIs(first.coordinator, second.coordinator)

    def test_cancel_is_ignored_after_publication_claim_while_clipboard_blocks(self):
        clipboard = BlockingClipboard()
        workflow = VoiceTranslationWorkflow(
            FakeProvider(),
            clipboard,
            VoiceTranslationConfig(),
            clock=FakeClock(),
            coordinator=VoiceTranslationPublicationCoordinator(),
        )
        outcomes = []
        worker = threading.Thread(
            target=lambda: outcomes.append(workflow.run(b"audio")),
            daemon=True,
        )
        worker.start()
        self.assertTrue(clipboard.publish_entered.wait(timeout=2))
        cancelled = workflow.state_machine.cancel()
        self.assertEqual(cancelled.phase, VoiceTranslationPhase.PUBLISHING)
        self.assertTrue(cancelled.publication_claimed)
        clipboard.publish_release.set()
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].phase, VoiceTranslationPhase.COMPLETED)
        self.assertEqual(
            outcomes[0].publication, VoiceTranslationPublication.PASTED
        )

    def test_phase_check_and_transition_are_atomic_against_cancel(self):
        machine = InterleavingStateMachine(
            VoiceTranslationConfig(), clock=FakeClock()
        )
        machine.begin()
        machine.begin_transcription()
        machine.block_translation = True
        transition = threading.Thread(
            target=lambda: machine.transcript_received("raw"),
            daemon=True,
        )
        transition.start()
        self.assertTrue(machine.transition_entered.wait(timeout=2))

        cancellation = threading.Thread(target=machine.cancel, daemon=True)
        cancellation.start()
        self.assertTrue(machine.cancel_entered.wait(timeout=2))
        machine.transition_release.set()
        transition.join(timeout=2)
        cancellation.join(timeout=2)

        self.assertFalse(transition.is_alive())
        self.assertFalse(cancellation.is_alive())
        self.assertEqual(machine.state.phase, VoiceTranslationPhase.CANCELLED)


if __name__ == "__main__":
    unittest.main()
