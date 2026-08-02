"""Headless tests for dictation, rewrite, and translation orchestration."""

from __future__ import annotations

import inspect
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import app
import workflows
from provider_types import RewriteResult, TranscriptionResult, TranslationResult
from workflows import (
    CancelDictation,
    CancelTranslation,
    ChooseTranslationLanguage,
    DismissMicrophoneUnavailable,
    MicrophoneUnavailableError,
    NoUsableAudioError,
    RecordingSnapshot,
    SelectionCapture,
    SelectionDisposition,
    SelectionTarget,
    StartDictation,
    StartRewrite,
    StartTranslation,
    StopDictation,
    WorkflowPhase,
    WorkflowService,
    WorkflowState,
)


class ImmediateScheduler:
    def call_soon(self, callback: Callable[[], None]) -> None:
        callback()

    def run_in_background(self, callback: Callable[[], None]) -> None:
        callback()


class ManualScheduler:
    def __init__(self):
        self.soon: list[Callable[[], None]] = []
        self.background: list[Callable[[], None]] = []

    def call_soon(self, callback: Callable[[], None]) -> None:
        self.soon.append(callback)

    def run_in_background(self, callback: Callable[[], None]) -> None:
        self.background.append(callback)


class ThreadScheduler:
    def __init__(self):
        self.threads = []

    def call_soon(self, callback: Callable[[], None]) -> None:
        callback()

    def run_in_background(self, callback: Callable[[], None]) -> None:
        thread = threading.Thread(target=callback, daemon=True)
        self.threads.append(thread)
        thread.start()

    def join(self):
        index = 0
        while index < len(self.threads):
            self.threads[index].join(timeout=1.0)
            if self.threads[index].is_alive():
                raise AssertionError("background workflow did not finish")
            index += 1


class FakeClock:
    def __init__(self):
        self.now = 10.0

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeProvider:
    def __init__(self):
        self.transcription = "Transcribed"
        self.rewritten = "Rewritten"
        self.translated = "Translated"
        self.on_transcribe = None
        self.on_rewrite = None

    def transcribe(self, audio_source, mode, language):
        self.transcription_request = (audio_source, mode, language)
        if self.on_transcribe:
            self.on_transcribe()
        return TranscriptionResult(
            self.transcription, "gemini", "gemini-test"
        )

    def rewrite(self, text):
        self.rewrite_request = text
        if self.on_rewrite:
            self.on_rewrite()
        return RewriteResult(self.rewritten, "openai", "gpt-test")

    def translate(self, text, target_language):
        self.translation_request = (text, target_language)
        return TranslationResult(
            self.translated, "openai", "gpt-test", target_language
        )


class FakeAudio:
    def __init__(self):
        self.available = True
        self.present = True
        self.started = 0
        self.stopped = 0
        self.cancelled = 0
        self.completed = 0
        self.failures = []
        self.startup_waits = 0

    def microphone_available(self):
        return self.available

    def create_session(self):
        return self

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1
        if not self.present:
            raise NoUsableAudioError("no audio")
        return RecordingSnapshot(Path("recording.wav"), b"audio")

    def wait_until_started(self):
        self.startup_waits += 1

    def cancel(self):
        self.cancelled += 1

    def complete(self):
        self.completed += 1

    def fail(self, error):
        self.failures.append(error)


class BlockingAudio(FakeAudio):
    def __init__(self, start_error=None):
        super().__init__()
        self.start_error = start_error
        self.start_entered = threading.Event()
        self.start_release = threading.Event()
        self.start_terminal = threading.Event()
        self.wait_entered = threading.Event()
        self.order = []

    def start(self):
        self.started += 1
        self.order.append("start_entered")
        self.start_entered.set()
        if not self.start_release.wait(timeout=1.0):
            raise AssertionError("test did not release recording startup")
        if self.cancelled and self.start_error is None:
            self.start_error = RuntimeError("recording startup cancelled")
        self.order.append("start_terminal")
        self.start_terminal.set()
        if self.start_error is not None:
            raise self.start_error

    def wait_until_started(self):
        self.startup_waits += 1
        self.order.append("wait_until_started")
        self.wait_entered.set()
        if not self.start_terminal.wait(timeout=1.0):
            raise AssertionError("recording startup did not become terminal")
        if self.start_error is not None:
            raise self.start_error

    def stop(self):
        self.order.append("stop")
        return super().stop()

    def cancel(self):
        self.order.append("cancel")
        super().cancel()
        self.start_release.set()


class FakeClipboard:
    def __init__(self):
        self.window = 77
        self.executable = "editor.exe"
        self.text = "previous"
        self.selected = "Original"
        self.alt_down = False
        self.writes = []
        self.applied = []
        self.auto_pastes = []
        self.activations = []
        self.restores = []
        self.captures = []
        self.current_text = self.text
        self.dictation_outputs = []

    def capture_target(self):
        if self.window is None:
            return None
        return SelectionTarget(self.window, self.executable)

    def is_target_current(self, target):
        return target.window == self.window

    def capture_selection(self, target):
        self.captures.append(target)
        if not self.selected:
            return None
        self.current_text = self.selected
        return SelectionCapture(target, self.selected, self.text)

    def restore(self, capture):
        self.restores.append(capture.context)
        self.current_text = capture.context

    def apply_result(self, capture, result):
        self.writes.append(result)
        self.applied.append((capture, result))
        if self.is_target_current(capture.target) and self.selected == capture.text:
            return SelectionDisposition.PASTED
        return SelectionDisposition.COPIED

    def write_dictation_result(self, target, text):
        self.writes.append(text)
        self.current_text = text
        disposition = SelectionDisposition.COPIED
        if target is not None and self.is_target_current(target):
            self.auto_pastes.append(text)
            disposition = SelectionDisposition.PASTED
        self.dictation_outputs.append((target, text, disposition))
        return disposition

    def activate(self, target):
        self.activations.append(target.window)

    def alt_pressed(self):
        return self.alt_down


class FakeConfig:
    def recording_usage_context(self, mode):
        return {"mode": mode, "provider": "gemini"}


class FakeStatistics:
    def __init__(self):
        self.dictations = []
        self.rewrites = []
        self.translations = []

    def record_dictation(self, context, duration_seconds, result):
        self.dictations.append((context, duration_seconds, result))

    def record_rewrite(self, provider, model, source, result):
        self.rewrites.append((provider, model, source, result))

    def record_translation(
        self, provider, model, source, result, target_language
    ):
        self.translations.append(
            (provider, model, source, result, target_language)
        )


class WorkflowServiceTests(unittest.TestCase):
    def setUp(self):
        self.provider = FakeProvider()
        self.audio = FakeAudio()
        self.clipboard = FakeClipboard()
        self.config = FakeConfig()
        self.statistics = FakeStatistics()
        self.clock = FakeClock()
        self.scheduler = ImmediateScheduler()
        self.service = self.make_service()
        self.states = []
        self.service.subscribe(self.states.append)

    def make_service(self, scheduler=None):
        return WorkflowService(
            self.provider,
            self.audio,
            self.clipboard,
            self.config,
            self.statistics,
            scheduler or self.scheduler,
            self.clock,
        )

    def test_service_module_has_no_tk_dependency(self):
        source = inspect.getsource(workflows)
        self.assertNotIn("customtkinter", source)
        self.assertNotIn("tkinter", source)

    def test_app_microphone_error_implements_the_workflow_domain_contract(self):
        self.assertTrue(
            issubclass(
                app.MicrophoneUnavailableError,
                workflows.MicrophoneUnavailableError,
            )
        )

    def test_rewrite_runs_headlessly_and_records_the_result(self):
        self.assertTrue(self.service.dispatch(StartRewrite()))

        state = self.service.state
        self.assertEqual(state.phase, WorkflowPhase.COMPLETED)
        self.assertEqual(state.result_text, "Rewritten")
        self.assertIsNone(state.status_key)
        self.assertEqual(self.clipboard.writes, ["Rewritten"])
        self.assertEqual(len(self.clipboard.applied), 1)
        self.assertEqual(
            self.statistics.rewrites,
            [("openai", "gpt-test", "Original", "Rewritten")],
        )
        self.assertEqual(
            [state.phase for state in self.states],
            [
                WorkflowPhase.REWRITING,
                WorkflowPhase.PUBLISHING,
                WorkflowPhase.COMPLETED,
            ],
        )

    def test_rewrite_focus_change_after_safe_capture_keeps_copied_result(self):
        original_rewrite = self.provider.rewrite

        def change_focus(text):
            result = original_rewrite(text)
            self.clipboard.window = 88
            return result

        self.provider.rewrite = change_focus

        self.service.dispatch(StartRewrite())

        self.assertEqual(self.service.state.phase, WorkflowPhase.COMPLETED)
        self.assertEqual(self.service.state.status_key, "rewrite_copied")
        self.assertEqual(self.service.state.result_text, "Rewritten")
        self.assertEqual(self.clipboard.writes, ["Rewritten"])
        self.assertEqual(len(self.clipboard.applied), 1)
        self.assertEqual(self.clipboard.restores, ["previous"])
        self.assertEqual(
            self.statistics.rewrites,
            [("openai", "gpt-test", "Original", "Rewritten")],
        )

    def test_rewrite_focus_change_during_alt_wait_skips_capture_and_provider(self):
        alt_checks = 0

        def release_alt_after_focus_change():
            nonlocal alt_checks
            alt_checks += 1
            if alt_checks == 1:
                return True
            self.clipboard.window = 88
            return False

        self.clipboard.alt_pressed = release_alt_after_focus_change

        self.service.dispatch(StartRewrite())

        self.assertEqual(self.service.state.phase, WorkflowPhase.FAILED)
        self.assertEqual(self.service.state.status_key, "no_selection")
        self.assertFalse(hasattr(self.provider, "rewrite_request"))
        self.assertEqual(self.clipboard.captures, [])
        self.assertEqual(self.clipboard.applied, [])
        self.assertEqual(self.clipboard.restores, [])
        self.assertEqual(self.statistics.rewrites, [])

    def test_rewrite_focus_change_during_capture_restores_before_provider(self):
        capture_selection = self.clipboard.capture_selection

        def capture_then_change_focus(target):
            capture = capture_selection(target)
            self.clipboard.window = 88
            return capture

        self.clipboard.capture_selection = capture_then_change_focus

        self.service.dispatch(StartRewrite())

        self.assertEqual(self.service.state.phase, WorkflowPhase.FAILED)
        self.assertEqual(self.service.state.status_key, "no_selection")
        self.assertFalse(hasattr(self.provider, "rewrite_request"))
        self.assertEqual(len(self.clipboard.captures), 1)
        self.assertEqual(self.clipboard.applied, [])
        self.assertEqual(self.clipboard.restores, ["previous"])
        self.assertEqual(self.statistics.rewrites, [])

    def test_rewrite_provider_failure_restores_capture_without_statistics(self):
        self.provider.rewritten = ""

        self.service.dispatch(StartRewrite())

        self.assertEqual(self.service.state.phase, WorkflowPhase.FAILED)
        self.assertEqual(self.service.state.status_key, "rewrite_failed")
        self.assertEqual(self.clipboard.restores, ["previous"])
        self.assertEqual(self.statistics.rewrites, [])

    def test_rewrite_restores_before_blocked_provider_and_cancel_stays_safe(self):
        scheduler = ThreadScheduler()
        provider_entered = threading.Event()
        provider_release = threading.Event()

        def block_provider():
            provider_entered.set()
            if not provider_release.wait(timeout=1.0):
                raise AssertionError("test did not release rewrite provider")

        self.provider.on_rewrite = block_provider
        service = self.make_service(scheduler)

        self.assertTrue(service.dispatch(StartRewrite()))
        self.assertTrue(provider_entered.wait(timeout=1.0))
        self.assertEqual(self.clipboard.current_text, "previous")
        self.assertEqual(self.clipboard.restores, ["previous"])

        service.cancel_active()
        provider_release.set()
        scheduler.join()

        self.assertEqual(service.state.phase, WorkflowPhase.READY)
        self.assertEqual(self.clipboard.current_text, "previous")
        self.assertEqual(self.clipboard.applied, [])
        self.assertEqual(self.statistics.rewrites, [])

    def test_rewrite_cancel_during_capture_restores_once(self):
        scheduler = ThreadScheduler()
        capture_entered = threading.Event()
        capture_release = threading.Event()
        capture_selection = self.clipboard.capture_selection

        def block_capture(target):
            capture = capture_selection(target)
            capture_entered.set()
            if not capture_release.wait(timeout=1.0):
                raise AssertionError("test did not release rewrite capture")
            return capture

        self.clipboard.capture_selection = block_capture
        service = self.make_service(scheduler)

        self.assertTrue(service.dispatch(StartRewrite()))
        self.assertTrue(capture_entered.wait(timeout=1.0))
        self.assertEqual(self.clipboard.current_text, "Original")

        service.cancel_active()
        capture_release.set()
        scheduler.join()

        self.assertEqual(service.state.phase, WorkflowPhase.READY)
        self.assertEqual(self.clipboard.current_text, "previous")
        self.assertEqual(self.clipboard.restores, ["previous"])
        self.assertFalse(hasattr(self.provider, "rewrite_request"))
        self.assertEqual(self.clipboard.applied, [])
        self.assertEqual(self.statistics.rewrites, [])

    def test_rewrite_cancel_before_queued_publication_has_no_late_output(self):
        scheduler = ManualScheduler()
        service = self.make_service(scheduler)

        self.assertTrue(service.dispatch(StartRewrite()))
        scheduler.background.pop(0)()  # capture/provider worker
        self.assertEqual(service.state.phase, WorkflowPhase.PUBLISHING)
        # Deliver the barrier first.  It queues the external publication only
        # after the view has observed the non-cancellable phase.
        while scheduler.soon:
            scheduler.soon.pop(0)()
        self.assertEqual(len(scheduler.background), 1)

        service.cancel_active()
        scheduler.background.pop(0)()  # queued publication

        self.assertEqual(service.state.phase, WorkflowPhase.READY)
        self.assertEqual(self.clipboard.applied, [])
        self.assertEqual(self.clipboard.writes, [])
        self.assertEqual(self.statistics.rewrites, [])

    def test_translation_prepares_picker_then_translates_after_choice(self):
        picker_restores = []
        self.service.subscribe(
            lambda state: picker_restores.append(list(self.clipboard.restores))
            if state.phase is WorkflowPhase.TRANSLATION_PICKER
            else None
        )

        self.assertTrue(self.service.dispatch(StartTranslation()))
        self.assertEqual(
            self.service.state.phase, WorkflowPhase.TRANSLATION_PICKER
        )
        self.assertEqual(picker_restores, [["previous"]])
        self.assertEqual(self.clipboard.restores, ["previous"])
        self.assertEqual(self.clipboard.writes, [])

        self.assertTrue(
            self.service.dispatch(ChooseTranslationLanguage("de"))
        )

        self.assertEqual(self.provider.translation_request, ("Original", "de"))
        self.assertEqual(self.clipboard.activations, [77])
        self.assertEqual(self.service.state.phase, WorkflowPhase.COMPLETED)
        self.assertEqual(self.service.state.result_text, "Translated")
        self.assertEqual(
            self.statistics.translations,
            [("openai", "gpt-test", "Original", "Translated", "de")],
        )
        self.assertEqual(self.clipboard.restores, ["previous"])

    def test_translation_focus_change_during_capture_stops_before_picker(self):
        capture_selection = self.clipboard.capture_selection

        def capture_then_change_focus(target):
            capture = capture_selection(target)
            self.clipboard.window = 88
            return capture

        self.clipboard.capture_selection = capture_then_change_focus

        self.service.dispatch(StartTranslation())

        self.assertEqual(self.service.state.phase, WorkflowPhase.FAILED)
        self.assertEqual(self.service.state.status_key, "no_selection")
        self.assertFalse(hasattr(self.provider, "translation_request"))
        self.assertNotIn(
            WorkflowPhase.TRANSLATION_PICKER,
            [state.phase for state in self.states],
        )
        self.assertEqual(self.clipboard.applied, [])
        self.assertEqual(self.clipboard.restores, ["previous"])
        self.assertEqual(self.statistics.translations, [])

    def test_translation_cancel_during_capture_restores_once(self):
        scheduler = ThreadScheduler()
        capture_entered = threading.Event()
        capture_release = threading.Event()
        capture_selection = self.clipboard.capture_selection

        def block_capture(target):
            capture = capture_selection(target)
            capture_entered.set()
            if not capture_release.wait(timeout=1.0):
                raise AssertionError("test did not release translation capture")
            return capture

        self.clipboard.capture_selection = block_capture
        service = self.make_service(scheduler)
        states = []
        service.subscribe(states.append)

        self.assertTrue(service.dispatch(StartTranslation()))
        self.assertTrue(capture_entered.wait(timeout=1.0))
        self.assertEqual(self.clipboard.current_text, "Original")

        service.cancel_active()
        capture_release.set()
        scheduler.join()

        self.assertEqual(service.state.phase, WorkflowPhase.READY)
        self.assertEqual(self.clipboard.current_text, "previous")
        self.assertEqual(self.clipboard.restores, ["previous"])
        self.assertFalse(hasattr(self.provider, "translation_request"))
        self.assertNotIn(
            WorkflowPhase.TRANSLATION_PICKER,
            [state.phase for state in states],
        )
        self.assertEqual(self.clipboard.applied, [])
        self.assertEqual(self.statistics.translations, [])

    def test_translation_cancel_before_queued_publication_has_no_late_output(self):
        scheduler = ManualScheduler()
        service = self.make_service(scheduler)

        self.assertTrue(service.dispatch(StartTranslation()))
        scheduler.background.pop(0)()  # capture/restore worker
        self.assertEqual(service.state.phase, WorkflowPhase.TRANSLATION_PICKER)
        self.assertTrue(service.dispatch(ChooseTranslationLanguage("de")))
        scheduler.background.pop(0)()  # provider worker
        self.assertEqual(service.state.phase, WorkflowPhase.PUBLISHING)
        while scheduler.soon:
            scheduler.soon.pop(0)()
        self.assertEqual(len(scheduler.background), 1)

        service.cancel_active()
        scheduler.background.pop(0)()  # queued publication

        self.assertEqual(service.state.phase, WorkflowPhase.READY)
        self.assertEqual(self.clipboard.applied, [])
        self.assertEqual(self.clipboard.writes, [])
        self.assertEqual(self.statistics.translations, [])

    def test_rewrite_shutdown_does_not_wait_for_blocked_apply(self):
        scheduler = ThreadScheduler()
        service = self.make_service(scheduler)
        apply_entered = threading.Event()
        apply_release = threading.Event()
        apply_result = self.clipboard.apply_result

        def blocked_apply(capture, result):
            apply_entered.set()
            if not apply_release.wait(timeout=1.0):
                raise AssertionError("test did not release rewrite apply")
            return apply_result(capture, result)

        self.clipboard.apply_result = blocked_apply

        self.assertTrue(service.dispatch(StartRewrite()))
        self.assertTrue(apply_entered.wait(timeout=1.0))

        cancel_finished = threading.Event()

        def cancel_for_shutdown():
            service.cancel_active()
            cancel_finished.set()

        threading.Thread(target=cancel_for_shutdown, daemon=True).start()
        self.assertTrue(
            cancel_finished.wait(timeout=0.5),
            "shutdown cancellation waited behind the clipboard gateway",
        )
        apply_release.set()
        scheduler.join()

        self.assertEqual(service.state.phase, WorkflowPhase.READY)
        self.assertEqual(len(self.clipboard.applied), 1)
        self.assertEqual(len(self.statistics.rewrites), 1)

    def test_translation_shutdown_does_not_wait_for_blocked_apply(self):
        scheduler = ThreadScheduler()
        service = self.make_service(scheduler)
        apply_entered = threading.Event()
        apply_release = threading.Event()
        apply_result = self.clipboard.apply_result

        def blocked_apply(capture, result):
            apply_entered.set()
            if not apply_release.wait(timeout=1.0):
                raise AssertionError("test did not release translation apply")
            return apply_result(capture, result)

        self.clipboard.apply_result = blocked_apply

        self.assertTrue(service.dispatch(StartTranslation()))
        # The picker is delivered synchronously by the test scheduler.
        self.assertTrue(service.dispatch(ChooseTranslationLanguage("de")))
        self.assertTrue(apply_entered.wait(timeout=1.0))

        cancel_finished = threading.Event()

        def cancel_for_shutdown():
            service.cancel_active()
            cancel_finished.set()

        threading.Thread(target=cancel_for_shutdown, daemon=True).start()
        self.assertTrue(
            cancel_finished.wait(timeout=0.5),
            "shutdown cancellation waited behind the clipboard gateway",
        )
        apply_release.set()
        scheduler.join()

        self.assertEqual(service.state.phase, WorkflowPhase.READY)
        self.assertEqual(len(self.clipboard.applied), 1)
        self.assertEqual(len(self.statistics.translations), 1)

    def test_translation_blocks_rewrite_while_selection_is_preparing(self):
        scheduler = ManualScheduler()
        service = self.make_service(scheduler)

        self.assertTrue(service.dispatch(StartTranslation()))
        self.assertEqual(
            service.state.phase, WorkflowPhase.PREPARING_TRANSLATION
        )
        self.assertFalse(service.dispatch(StartRewrite()))

        scheduler.background.pop(0)()
        self.assertEqual(service.state.phase, WorkflowPhase.TRANSLATION_PICKER)

    def test_translation_picker_cancel_restores_target_and_releases_flow(self):
        self.service.dispatch(StartTranslation())

        self.assertTrue(self.service.dispatch(CancelTranslation()))

        self.assertEqual(self.clipboard.activations, [77])
        self.assertEqual(self.clipboard.restores, ["previous"])
        self.assertEqual(self.service.state.phase, WorkflowPhase.READY)
        self.assertEqual(self.statistics.translations, [])

    def test_dictation_owns_start_stop_provider_and_statistics_sequence(self):
        self.assertTrue(
            self.service.dispatch(
                StartDictation(SelectionTarget(77, "editor.exe"), "prompt", "pt")
            )
        )
        self.clock.now = 12.5
        self.assertTrue(self.service.dispatch(StopDictation()))

        self.assertEqual(self.audio.started, 1)
        self.assertEqual(self.audio.startup_waits, 1)
        self.assertEqual(self.audio.stopped, 1)
        self.assertEqual(self.audio.completed, 1)
        self.assertEqual(
            self.provider.transcription_request,
            (
                RecordingSnapshot(Path("recording.wav"), b"audio"),
                "prompt",
                "pt",
            ),
        )
        self.assertEqual(self.clipboard.auto_pastes, ["Transcribed"])
        self.assertEqual(
            self.clipboard.dictation_outputs,
            [
                (
                    SelectionTarget(77, "editor.exe"),
                    "Transcribed",
                    SelectionDisposition.PASTED,
                )
            ],
        )
        self.assertEqual(
            self.statistics.dictations,
            [({"mode": "prompt", "provider": "gemini"}, 2.5, "Transcribed")],
        )
        self.assertEqual(self.service.state.phase, WorkflowPhase.COMPLETED)

    def test_dictation_focus_change_during_transcription_uses_copied_fallback(self):
        self.provider.on_transcribe = lambda: setattr(
            self.clipboard, "window", 88
        )

        self.service.dispatch(
            StartDictation(SelectionTarget(77, "editor.exe"), "prompt", "en")
        )
        self.service.dispatch(StopDictation())

        self.assertEqual(self.clipboard.auto_pastes, [])
        self.assertEqual(self.clipboard.writes, ["Transcribed"])
        self.assertEqual(
            self.clipboard.dictation_outputs,
            [
                (
                    SelectionTarget(77, "editor.exe"),
                    "Transcribed",
                    SelectionDisposition.COPIED,
                )
            ],
        )
        self.assertEqual(self.service.state.phase, WorkflowPhase.COMPLETED)

    def test_immediate_stop_waits_for_blocked_startup_before_stopping(self):
        scheduler = ThreadScheduler()
        self.audio = BlockingAudio()
        service = self.make_service(scheduler)

        self.assertTrue(
            service.dispatch(
                StartDictation(SelectionTarget(77, "editor.exe"), "prompt", "en")
            )
        )
        self.assertTrue(self.audio.start_entered.wait(timeout=1.0))
        self.assertTrue(service.dispatch(StopDictation()))
        self.assertTrue(self.audio.wait_entered.wait(timeout=1.0))
        self.assertEqual(self.audio.stopped, 0)

        self.audio.start_release.set()
        scheduler.join()

        self.assertEqual(self.audio.started, 1)
        self.assertEqual(self.audio.startup_waits, 1)
        self.assertEqual(self.audio.stopped, 1)
        self.assertLess(
            self.audio.order.index("start_terminal"),
            self.audio.order.index("stop"),
        )
        self.assertEqual(service.state.phase, WorkflowPhase.COMPLETED)

    def test_immediate_stop_propagates_blocked_startup_failure(self):
        scheduler = ThreadScheduler()
        self.audio = BlockingAudio(RuntimeError("startup failed"))
        service = self.make_service(scheduler)

        service.dispatch(
            StartDictation(SelectionTarget(77, "editor.exe"), "prompt", "en")
        )
        self.assertTrue(self.audio.start_entered.wait(timeout=1.0))
        service.dispatch(StopDictation())
        self.assertTrue(self.audio.wait_entered.wait(timeout=1.0))
        self.audio.start_release.set()
        scheduler.join()

        self.assertEqual(service.state.phase, WorkflowPhase.FAILED)
        self.assertEqual(service.state.status_key, "error")
        self.assertEqual(self.audio.stopped, 0)
        self.assertEqual(len(self.audio.failures), 1)
        self.assertFalse(hasattr(self.provider, "transcription_request"))
        self.assertEqual(self.clipboard.auto_pastes, [])
        self.assertEqual(self.statistics.dictations, [])

    def test_generic_startup_failure_is_not_reported_as_microphone_unavailable(self):
        scheduler = ThreadScheduler()
        self.audio = BlockingAudio(RuntimeError("SoX permission denied"))
        service = self.make_service(scheduler)

        service.dispatch(
            StartDictation(SelectionTarget(77, "editor.exe"), "prompt", "en")
        )
        self.assertTrue(self.audio.start_entered.wait(timeout=1.0))
        self.audio.start_release.set()
        scheduler.join()

        self.assertEqual(service.state.phase, WorkflowPhase.FAILED)
        self.assertEqual(service.state.status_key, "error")

    def test_typed_microphone_startup_failure_reports_microphone_unavailable(self):
        scheduler = ThreadScheduler()
        self.audio = BlockingAudio(MicrophoneUnavailableError("no input"))
        service = self.make_service(scheduler)

        service.dispatch(
            StartDictation(SelectionTarget(77, "editor.exe"), "prompt", "en")
        )
        self.assertTrue(self.audio.start_entered.wait(timeout=1.0))
        self.audio.start_release.set()
        scheduler.join()

        self.assertEqual(
            service.state.phase, WorkflowPhase.MICROPHONE_UNAVAILABLE
        )
        self.assertIsInstance(self.audio.failures[0], MicrophoneUnavailableError)

    def test_immediate_stop_preserves_typed_microphone_startup_failure(self):
        scheduler = ThreadScheduler()
        self.audio = BlockingAudio(MicrophoneUnavailableError("no input"))
        service = self.make_service(scheduler)

        service.dispatch(
            StartDictation(SelectionTarget(77, "editor.exe"), "prompt", "en")
        )
        self.assertTrue(self.audio.start_entered.wait(timeout=1.0))
        self.assertTrue(service.dispatch(StopDictation()))
        self.assertTrue(self.audio.wait_entered.wait(timeout=1.0))
        self.audio.start_release.set()
        scheduler.join()

        self.assertEqual(
            service.state.phase, WorkflowPhase.MICROPHONE_UNAVAILABLE
        )

    def test_audio_owner_rejection_keeps_dictation_ready_without_worker(self):
        scheduler = ManualScheduler()
        self.audio.create_session = lambda: None
        service = self.make_service(scheduler)

        self.assertFalse(
            service.dispatch(
                StartDictation(SelectionTarget(77, "editor.exe"), "prompt", "en")
            )
        )

        self.assertEqual(service.state, WorkflowState())
        self.assertEqual(scheduler.background, [])
        self.assertFalse(hasattr(self.provider, "transcription_request"))
        self.assertEqual(self.statistics.dictations, [])

    def test_cancel_during_blocked_startup_prevents_late_stop_or_provider(self):
        scheduler = ThreadScheduler()
        self.audio = BlockingAudio()
        service = self.make_service(scheduler)

        service.dispatch(
            StartDictation(SelectionTarget(77, "editor.exe"), "prompt", "en")
        )
        self.assertTrue(self.audio.start_entered.wait(timeout=1.0))
        self.assertTrue(service.dispatch(CancelDictation()))
        scheduler.join()

        self.assertEqual(service.state.phase, WorkflowPhase.READY)
        self.assertEqual(self.audio.stopped, 0)
        self.assertGreaterEqual(self.audio.cancelled, 1)
        self.assertEqual(self.audio.failures, [])
        self.assertLess(
            self.audio.order.index("cancel"),
            self.audio.order.index("start_terminal"),
        )
        self.assertFalse(hasattr(self.provider, "transcription_request"))
        self.assertEqual(self.clipboard.auto_pastes, [])
        self.assertEqual(self.statistics.dictations, [])

    def test_no_usable_audio_is_a_distinct_failure(self):
        self.audio.present = False
        self.service.dispatch(
            StartDictation(SelectionTarget(77, "editor.exe"), "prompt", "en")
        )

        self.service.dispatch(StopDictation())

        self.assertEqual(self.service.state.phase, WorkflowPhase.FAILED)
        self.assertEqual(self.service.state.status_key, "no_audio")
        self.assertEqual(len(self.audio.failures), 1)
        self.assertFalse(hasattr(self.provider, "transcription_request"))

    def test_microphone_unavailable_can_be_dismissed_without_a_session(self):
        self.audio.available = False

        self.service.dispatch(
            StartDictation(SelectionTarget(77, "editor.exe"), "prompt", "en")
        )

        self.assertEqual(
            self.service.state.phase, WorkflowPhase.MICROPHONE_UNAVAILABLE
        )
        self.assertEqual(self.audio.started, 0)
        self.assertTrue(
            self.service.dispatch(DismissMicrophoneUnavailable())
        )
        self.assertEqual(self.service.state.phase, WorkflowPhase.READY)

    def test_cancelled_recording_start_cannot_restore_stale_state(self):
        scheduler = ManualScheduler()
        service = self.make_service(scheduler)
        states = []
        service.subscribe(states.append)

        self.assertTrue(
            service.dispatch(
                StartDictation(SelectionTarget(77, "editor.exe"), "prompt", "en")
            )
        )
        self.assertTrue(service.dispatch(CancelDictation()))
        self.assertEqual(service.state.phase, WorkflowPhase.READY)

        start_worker = scheduler.background.pop(0)
        start_worker()

        self.assertEqual(service.state.phase, WorkflowPhase.READY)
        self.assertEqual(self.audio.started, 1)
        self.assertGreaterEqual(self.audio.cancelled, 1)
        self.assertEqual(self.statistics.dictations, [])
        self.assertEqual(self.clipboard.auto_pastes, [])

    def test_stale_rewrite_completion_cannot_touch_clipboard_or_statistics(self):
        self.provider.on_rewrite = self.service.cancel_active

        self.assertTrue(self.service.dispatch(StartRewrite()))

        self.assertEqual(self.service.state.phase, WorkflowPhase.READY)
        self.assertEqual(self.clipboard.writes, [])
        self.assertEqual(self.clipboard.applied, [])
        self.assertEqual(self.statistics.rewrites, [])

    def test_finished_dictation_waits_for_a_delayed_clipboard_worker(self):
        scheduler = ManualScheduler()
        service = self.make_service(scheduler)
        service.dispatch(
            StartDictation(SelectionTarget(77, "editor.exe"), "prompt", "en")
        )
        scheduler.background.pop(0)()  # recording start
        self.clock.now = 11.0
        service.dispatch(StopDictation())
        scheduler.background.pop(0)()  # stop/provider worker
        while scheduler.soon:
            scheduler.soon.pop(0)()  # terminal delivery, then publication queue
        operation_id = service.state.operation_id
        self.assertEqual(service.state.phase, WorkflowPhase.COMPLETED)

        self.assertTrue(service.finish(operation_id))
        scheduler.background.pop(0)()  # delayed clipboard delivery
        while scheduler.soon:
            scheduler.soon.pop(0)()  # deferred READY delivery

        self.assertEqual(self.clipboard.auto_pastes, ["Transcribed"])
        self.assertEqual(len(self.statistics.dictations), 1)
        self.assertEqual(service.state.phase, WorkflowPhase.READY)

    def test_terminal_listener_can_finish_before_dictation_publication_claim(self):
        scheduler = ManualScheduler()
        service = self.make_service(scheduler)
        finished = []

        def finish_on_terminal(state):
            if state.phase is WorkflowPhase.COMPLETED:
                finished.append(service.finish(state.operation_id))

        service.subscribe(finish_on_terminal)
        service.dispatch(
            StartDictation(SelectionTarget(77, "editor.exe"), "prompt", "en")
        )
        scheduler.background.pop(0)()  # recording start
        service.dispatch(StopDictation())
        scheduler.background.pop(0)()  # stop/provider worker
        while scheduler.soon:
            scheduler.soon.pop(0)()  # terminal listener queues publication
        scheduler.background.pop(0)()  # publication claimed after listener
        while scheduler.soon:
            scheduler.soon.pop(0)()  # deferred READY delivery

        self.assertEqual(finished, [True])
        self.assertEqual(self.clipboard.auto_pastes, ["Transcribed"])
        self.assertEqual(len(self.statistics.dictations), 1)
        self.assertEqual(service.state.phase, WorkflowPhase.READY)

    def test_dictation_cancel_wins_before_queued_publication(self):
        scheduler = ManualScheduler()
        service = self.make_service(scheduler)
        target = SelectionTarget(77, "editor.exe")

        service.dispatch(StartDictation(target, "prompt", "en"))
        scheduler.background.pop(0)()  # recording start
        service.dispatch(StopDictation())
        scheduler.background.pop(0)()  # stop/provider worker
        while scheduler.soon:
            scheduler.soon.pop(0)()  # terminal delivery, then publication queue
        self.assertEqual(service.state.phase, WorkflowPhase.COMPLETED)
        self.assertEqual(len(scheduler.background), 1)

        # Invalidate the operation before the queued external publication.
        service.cancel_active()
        scheduler.background.pop(0)()  # dictation publication

        self.assertEqual(service.state.phase, WorkflowPhase.READY)
        self.assertEqual(self.clipboard.auto_pastes, [])
        self.assertEqual(self.clipboard.writes, [])
        self.assertEqual(self.statistics.dictations, [])

    def test_dictation_shutdown_does_not_wait_for_blocked_clipboard_write(self):
        scheduler = ThreadScheduler()
        service = self.make_service(scheduler)
        write_entered = threading.Event()
        write_release = threading.Event()
        write_result = self.clipboard.write_dictation_result

        def blocked_write(target, text):
            write_entered.set()
            if not write_release.wait(timeout=1.0):
                raise AssertionError("test did not release dictation write")
            return write_result(target, text)

        self.clipboard.write_dictation_result = blocked_write
        target = SelectionTarget(77, "editor.exe")
        service.dispatch(StartDictation(target, "prompt", "en"))
        service.dispatch(StopDictation())
        self.assertTrue(write_entered.wait(timeout=1.0))

        cancel_finished = threading.Event()

        def cancel_for_shutdown():
            service.cancel_active()
            cancel_finished.set()

        threading.Thread(target=cancel_for_shutdown, daemon=True).start()
        self.assertTrue(
            cancel_finished.wait(timeout=0.5),
            "shutdown cancellation waited behind the clipboard gateway",
        )
        write_release.set()
        scheduler.join()

        self.assertEqual(service.state.phase, WorkflowPhase.READY)
        self.assertEqual(self.clipboard.auto_pastes, ["Transcribed"])
        self.assertEqual(len(self.statistics.dictations), 1)

    def test_app_escape_during_blocked_dictation_publication_stays_terminal(self):
        scheduler = ThreadScheduler()
        service = self.make_service(scheduler)
        write_entered = threading.Event()
        write_release = threading.Event()
        write_result = self.clipboard.write_dictation_result

        def blocked_write(target, text):
            write_entered.set()
            if not write_release.wait(timeout=1.0):
                raise AssertionError("test did not release dictation write")
            return write_result(target, text)

        self.clipboard.write_dictation_result = blocked_write
        target = SelectionTarget(77, "editor.exe")
        service.dispatch(StartDictation(target, "prompt", "en"))
        service.dispatch(StopDictation())
        self.assertTrue(write_entered.wait(timeout=1.0))
        operation_id = service.state.operation_id
        harness = SimpleNamespace(
            _workflow_service=service,
            _translation_picker=None,
            app_state="success",
        )

        app.App._on_escape(harness)
        app.App._cancel(harness)

        self.assertEqual(service.state.phase, WorkflowPhase.COMPLETED)
        self.assertEqual(self.clipboard.dictation_outputs, [])
        self.assertIsNotNone(service._session)

        write_release.set()
        scheduler.join()
        self.assertEqual(service.state.phase, WorkflowPhase.COMPLETED)
        self.assertTrue(service.finish(operation_id))
        self.assertEqual(service.state.phase, WorkflowPhase.READY)
        self.assertEqual(self.clipboard.auto_pastes, ["Transcribed"])

    def test_app_escape_during_blocked_selection_publication_stays_publishing(self):
        scheduler = ThreadScheduler()
        service = self.make_service(scheduler)
        apply_entered = threading.Event()
        apply_release = threading.Event()
        apply_result = self.clipboard.apply_result

        def blocked_apply(capture, result):
            apply_entered.set()
            if not apply_release.wait(timeout=1.0):
                raise AssertionError("test did not release selection apply")
            return apply_result(capture, result)

        self.clipboard.apply_result = blocked_apply
        service.dispatch(StartRewrite())
        self.assertTrue(apply_entered.wait(timeout=1.0))
        operation_id = service.state.operation_id
        harness = SimpleNamespace(
            _workflow_service=service,
            _translation_picker=None,
            app_state="rewriting",
        )

        app.App._on_escape(harness)
        app.App._cancel(harness)

        self.assertEqual(service.state.phase, WorkflowPhase.PUBLISHING)
        self.assertIsNotNone(service._session)
        self.assertEqual(self.clipboard.applied, [])

        apply_release.set()
        scheduler.join()
        self.assertEqual(service.state.phase, WorkflowPhase.COMPLETED)
        self.assertTrue(service.finish(operation_id))
        self.assertEqual(service.state.phase, WorkflowPhase.READY)
        self.assertEqual(len(self.clipboard.applied), 1)
        self.assertEqual(len(self.statistics.rewrites), 1)

    def test_app_renders_the_non_cancellable_publishing_barrier(self):
        set_state = Mock()
        harness = SimpleNamespace(_set_state=set_state)

        app.App._on_workflow_state(
            harness,
            WorkflowState(
                phase=WorkflowPhase.PUBLISHING,
                operation_id=1,
                kind=workflows.WorkflowKind.REWRITE,
            ),
        )

        set_state.assert_called_once_with("processing")

    def test_terminal_operation_releases_only_matching_operation_id(self):
        self.service.dispatch(StartRewrite())
        operation_id = self.service.state.operation_id

        self.assertFalse(self.service.finish(operation_id + 1))
        self.assertEqual(self.service.state.phase, WorkflowPhase.COMPLETED)
        self.assertTrue(self.service.finish(operation_id))
        self.assertEqual(self.service.state.phase, WorkflowPhase.READY)

    def test_finish_publishes_ready_after_completed_or_failed_animation(self):
        for rewritten, terminal_phase in (
            ("Rewritten", WorkflowPhase.COMPLETED),
            ("", WorkflowPhase.FAILED),
        ):
            with self.subTest(terminal_phase=terminal_phase):
                self.provider.rewritten = rewritten
                self.service.dispatch(StartRewrite())
                operation_id = self.service.state.operation_id
                self.assertEqual(self.states[-1].phase, terminal_phase)

                self.assertTrue(self.service.finish(operation_id))

                self.assertEqual(self.states[-1].phase, WorkflowPhase.READY)
                self.assertEqual(self.states[-1].operation_id, 0)
                self.assertEqual(
                    [state.phase for state in self.states[-2:]],
                    [terminal_phase, WorkflowPhase.READY],
                )


if __name__ == "__main__":
    unittest.main()
