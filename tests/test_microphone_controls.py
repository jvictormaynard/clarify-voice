import unittest
from types import SimpleNamespace

from microphone_controls import (
    MicrophoneDevice,
    MicrophoneInventory,
    MicrophoneSelectionState,
    MicrophoneSettings,
    NonMonotonicTimestampError,
    RecordingBoundaryPolicy,
    RecordingBoundaryReason,
    RecordingBoundaryState,
    RecordingControls,
    RecordingControlsError,
    SilenceVADPolicy,
    SoundDeviceMicrophoneInventory,
    VADSettings,
    stable_microphone_id,
)


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def monotonic(self):
        return self.now


class MicrophoneControlsTests(unittest.TestCase):
    def test_identity_ignores_transient_enumeration_index(self):
        first = MicrophoneDevice.from_mapping({
            "name": "USB Mic",
            "host_api": "Windows WASAPI",
            "index": 2,
            "max_input_channels": 1,
        })
        after_hot_plug = MicrophoneDevice.from_mapping({
            "name": "USB Mic",
            "host_api": "Windows WASAPI",
            "index": 0,
            "max_input_channels": 1,
        })
        self.assertEqual(first.stable_id, after_hot_plug.stable_id)
        self.assertEqual(first.backend_index, 2)
        self.assertEqual(after_hot_plug.backend_index, 0)
        self.assertNotIn("backend_index", first.to_mapping())
        self.assertEqual(
            first.stable_id,
            stable_microphone_id("USB Mic", "Windows WASAPI"),
        )

    def test_native_identity_distinguishes_same_named_endpoints(self):
        first = MicrophoneDevice.from_mapping({
            "name": "Headset",
            "host_api": "WASAPI",
            "native_id": "endpoint-a",
            "max_input_channels": 1,
        })
        second = MicrophoneDevice.from_mapping({
            "name": "Headset",
            "host_api": "WASAPI",
            "native_id": "endpoint-b",
            "max_input_channels": 1,
        })
        self.assertNotEqual(first.stable_id, second.stable_id)

    def test_selection_uses_requested_device_when_available(self):
        devices = MicrophoneInventory.from_records([
            {"name": "Default", "max_input_channels": 1, "is_default": True},
            {"name": "Headset", "max_input_channels": 1},
        ])
        requested = devices.devices[1]
        selection = devices.resolve(requested.stable_id)
        self.assertEqual(selection.state, MicrophoneSelectionState.SELECTED)
        self.assertTrue(selection.can_record)
        self.assertEqual(selection.selected_id, requested.stable_id)

    def test_stale_selection_reports_visible_default_fallback(self):
        inventory = MicrophoneInventory.from_records([
            {"name": "Current default", "max_input_channels": 1, "is_default": True},
        ])
        selection = inventory.resolve("mic-v1-stale")
        self.assertEqual(selection.state, MicrophoneSelectionState.FALLBACK_DEFAULT)
        self.assertEqual(selection.reason, "saved_device_unavailable")
        self.assertTrue(selection.can_record)

    def test_sparse_backend_default_handle_is_not_used_as_tuple_offset(self):
        inventory = MicrophoneInventory.from_records(
            [
                {
                    "name": "Input seven",
                    "index": 7,
                    "max_input_channels": 1,
                },
                {
                    "name": "Input three",
                    "index": 3,
                    "max_input_channels": 1,
                },
            ],
            default_index=7,
        )
        self.assertEqual(inventory.default_id, inventory.devices[0].stable_id)

    def test_ambiguous_saved_identity_is_reported_as_fallback(self):
        inventory = MicrophoneInventory.from_records(
            [
                {
                    "name": "Same name",
                    "host_api": "WASAPI",
                    "index": 7,
                    "max_input_channels": 1,
                },
                {
                    "name": "Same name",
                    "host_api": "WASAPI",
                    "index": 3,
                    "max_input_channels": 1,
                },
                {
                    "name": "Current default",
                    "native_id": "default",
                    "is_default": True,
                    "max_input_channels": 1,
                },
            ]
        )
        ambiguous_id = inventory.devices[0].stable_id
        selection = inventory.resolve(ambiguous_id)
        self.assertEqual(selection.state, MicrophoneSelectionState.FALLBACK_DEFAULT)
        self.assertEqual(selection.reason, "saved_device_unavailable")
        self.assertEqual(selection.device.name, "Current default")

    def test_missing_default_is_explicitly_unavailable_not_arbitrary(self):
        inventory = MicrophoneInventory.from_records([
            {"name": "Unmarked input", "max_input_channels": 1},
        ])
        selection = inventory.resolve("mic-v1-stale")
        self.assertEqual(selection.state, MicrophoneSelectionState.UNAVAILABLE)
        self.assertFalse(selection.can_record)
        self.assertIsNone(selection.device)

    def test_unusable_default_does_not_fallback(self):
        inventory = MicrophoneInventory.from_records([
            {"name": "Disabled", "max_input_channels": 0, "is_default": True},
            {"name": "Other input", "max_input_channels": 1},
        ])
        self.assertEqual(
            inventory.resolve("mic-v1-stale").state,
            MicrophoneSelectionState.UNAVAILABLE,
        )

    def test_settings_round_trip_and_legacy_alias(self):
        settings = MicrophoneSettings("mic-v1-abc")
        self.assertEqual(
            MicrophoneSettings.from_mapping(settings.to_mapping()), settings)
        self.assertEqual(
            MicrophoneSettings.from_mapping({"microphone_id": "mic-v1-legacy"}).selected_id,
            "mic-v1-legacy",
        )
        self.assertIsNone(MicrophoneSettings.from_mapping(None).selected_id)

    def test_sounddevice_adapter_translates_default_index_without_persisting_it(self):
        records = [
            {"name": "Other", "hostapi": "MME", "max_input_channels": 1},
            {"name": "Default", "hostapi": "MME", "max_input_channels": 1},
        ]
        fake = SimpleNamespace(
            query_devices=lambda: records,
            default=SimpleNamespace(device=(1, -1)),
        )
        inventory = SoundDeviceMicrophoneInventory(fake).snapshot()
        self.assertEqual(inventory.default_id, inventory.devices[1].stable_id)
        self.assertNotIn("index", inventory.devices[1].to_mapping())

    def test_sounddevice_adapter_accepts_pair_like_default(self):
        class InputOutputPair:
            input = 1

            def __getitem__(self, index):
                if index == 0:
                    return self.input
                if index == 1:
                    return -1
                raise IndexError(index)

        fake = SimpleNamespace(
            query_devices=lambda: [
                {"name": "Other", "max_input_channels": 1},
                {"name": "Default", "max_input_channels": 1},
            ],
            default=SimpleNamespace(device=InputOutputPair()),
        )
        inventory = SoundDeviceMicrophoneInventory(fake).snapshot()
        self.assertEqual(inventory.default_id, inventory.devices[1].stable_id)

    def test_sounddevice_adapter_accepts_single_default_mapping(self):
        fake = SimpleNamespace(
            query_devices=lambda: {
                "name": "Default input",
                "max_input_channels": 1,
                "hostapi": "MME",
            },
            default=SimpleNamespace(device=(0, -1)),
        )

        inventory = SoundDeviceMicrophoneInventory(fake).snapshot()

        self.assertEqual(len(inventory.devices), 1)
        self.assertEqual(inventory.devices[0].name, "Default input")
        self.assertEqual(inventory.default_id, inventory.devices[0].stable_id)

    def test_sounddevice_adapter_resolves_numeric_host_api_name(self):
        fake = SimpleNamespace(
            query_devices=lambda: [
                {
                    "name": "Stable mic",
                    "hostapi": 0,
                    "index": 7,
                    "max_input_channels": 1,
                },
            ],
            query_hostapis=lambda: [{"name": "Windows WASAPI"}],
            default=SimpleNamespace(device=(7, -1)),
        )
        inventory = SoundDeviceMicrophoneInventory(fake).snapshot()
        self.assertEqual(inventory.devices[0].host_api, "Windows WASAPI")
        self.assertEqual(
            inventory.devices[0].stable_id,
            stable_microphone_id("Stable mic", "Windows WASAPI"),
        )

    def test_sounddevice_enumeration_failure_is_safe_unavailable_state(self):
        class Broken:
            def query_devices(self):
                raise RuntimeError("PortAudio unavailable")

        inventory = SoundDeviceMicrophoneInventory(Broken()).snapshot()
        self.assertEqual(inventory.error_code, "enumeration_failed")
        self.assertEqual(inventory.resolve().state, MicrophoneSelectionState.UNAVAILABLE)

    def test_defaults_do_not_change_existing_recording_behavior(self):
        controls = RecordingControls.defaults()
        self.assertIsNone(controls.max_duration_seconds)
        self.assertFalse(controls.vad.enabled)
        self.assertEqual(controls, RecordingControls.from_mapping(controls.to_mapping()))

    def test_controls_reject_future_schema_and_unsafe_durations(self):
        with self.assertRaises(RecordingControlsError):
            RecordingControls.from_mapping({"schema_version": 99})
        with self.assertRaises(RecordingControlsError):
            RecordingControls(max_duration_seconds=2, warning_seconds=3)
        with self.assertRaises(RecordingControlsError):
            VADSettings(enabled=True, silence_duration_seconds=0)

    def test_fake_clock_max_duration_warns_then_stops(self):
        clock = FakeClock()
        policy = RecordingBoundaryPolicy(
            RecordingControls(max_duration_seconds=10, warning_seconds=2),
            clock=clock,
        )
        self.assertEqual(policy.start().state, RecordingBoundaryState.ACTIVE)
        clock.now = 8
        warning = policy.observe(input_level=0)
        self.assertTrue(warning.warning)
        self.assertFalse(warning.should_stop)
        clock.now = 10
        stopped = policy.observe(input_level=0)
        self.assertTrue(stopped.should_stop)
        self.assertEqual(stopped.reason, RecordingBoundaryReason.MAX_DURATION)
        self.assertEqual(policy.observe().reason, RecordingBoundaryReason.MAX_DURATION)

    def test_duration_observation_does_not_advance_vad_clock(self):
        clock = FakeClock()
        policy = RecordingBoundaryPolicy(
            RecordingControls(
                max_duration_seconds=10,
                vad=VADSettings(enabled=True),
            ),
            clock=clock,
        )
        policy.start()

        clock.now = 4
        duration = policy.observe_duration()
        self.assertFalse(duration.should_stop)
        self.assertIsNone(duration.vad)

        callback = policy.observe(input_level=1.0)
        self.assertIsNotNone(callback.vad)
        self.assertTrue(callback.vad.speech_detected)

        clock.now = 10
        stopped = policy.observe_duration()
        self.assertEqual(stopped.reason, RecordingBoundaryReason.MAX_DURATION)

    def test_vad_requires_minimum_speech_and_continuous_silence(self):
        settings = VADSettings(
            enabled=True,
            level_threshold=0.2,
            minimum_speech_seconds=0.5,
            silence_duration_seconds=0.8,
        )
        policy = SilenceVADPolicy(settings)
        policy.start(0)
        self.assertFalse(policy.observe(0, 0.3).should_stop)
        # Only 0.3 seconds of actual speech: the first quiet interval must not
        # publish a partial recording.
        self.assertFalse(policy.observe(0.3, 0).should_stop)
        self.assertFalse(policy.observe(1.1, 0).should_stop)
        # A second, sufficiently long utterance may stop after its own silence.
        self.assertFalse(policy.observe(1.2, 0.3).should_stop)
        self.assertFalse(policy.observe(1.8, 0).should_stop)
        stopped = policy.observe(2.6, 0)
        self.assertTrue(stopped.should_stop)
        self.assertEqual(stopped.reason, RecordingBoundaryReason.SILENCE)

    def test_vad_does_not_stop_when_disabled_or_without_speech(self):
        disabled = SilenceVADPolicy(VADSettings())
        disabled.start(0)
        self.assertFalse(disabled.observe(100, 0).should_stop)
        enabled = SilenceVADPolicy(VADSettings(enabled=True, silence_duration_seconds=0.1))
        enabled.start(0)
        self.assertFalse(enabled.observe(0, 0).should_stop)
        self.assertFalse(enabled.observe(10, 0).should_stop)

    def test_terminal_vad_observations_preserve_monotonic_order(self):
        policy = SilenceVADPolicy(
            VADSettings(enabled=True, minimum_speech_seconds=0, silence_duration_seconds=1))
        policy.start(0)
        policy.observe(0, 1)
        self.assertFalse(policy.observe(1, 0).should_stop)
        self.assertTrue(policy.observe(2, 0).should_stop)
        policy.observe(10, 0)
        with self.assertRaises(NonMonotonicTimestampError):
            policy.observe(5, 0)

    def test_policies_reject_clock_regression(self):
        policy = SilenceVADPolicy(VADSettings(enabled=True))
        policy.start(2)
        policy.observe(3, 0.5)
        with self.assertRaises(NonMonotonicTimestampError):
            policy.observe(2.9, 0)
        boundary = RecordingBoundaryPolicy(RecordingControls(), clock=FakeClock(2))
        boundary.start()
        with self.assertRaises(NonMonotonicTimestampError):
            boundary.observe(1)
        boundary.start(0)
        boundary.cancel(3)
        with self.assertRaises(NonMonotonicTimestampError):
            boundary.observe(2)

    def test_cancellation_and_device_loss_are_explicit_terminal_states(self):
        policy = RecordingBoundaryPolicy(RecordingControls())
        policy.start(10)
        cancelled = policy.cancel(11)
        self.assertEqual(cancelled.state, RecordingBoundaryState.CANCELLED)
        self.assertEqual(cancelled.reason, RecordingBoundaryReason.CANCELLED)
        self.assertEqual(policy.device_lost(12).state, RecordingBoundaryState.CANCELLED)


if __name__ == "__main__":
    unittest.main()
