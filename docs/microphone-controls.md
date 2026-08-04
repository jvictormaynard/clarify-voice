# Microphone selection and recording controls (issue #52)

This change adds the UI-independent foundation for microphone selection and
recording boundaries. It intentionally does not connect a new device picker or
VAD loop to `app.py`; the existing `Recorder`/`RecordingSession` lifecycle and
its cancellation and temporary-WAV cleanup remain unchanged.

## Device identity and inventory

`microphone_controls.py` normalizes backend records into
`MicrophoneDevice` values. A persisted selection uses `stable_id`, never the
PortAudio enumeration index. If a backend exposes a native endpoint/GUID, the
identity is derived from that value; otherwise the normalized host API and
device name form a deterministic best-effort identity. The index may still be
used by a future adapter to open a stream and is retained only in the current
snapshot; it is not written to settings or included in the stable hash.

Two indistinguishable devices with the same fallback identity are deliberately
ambiguous. The inventory will not select either one implicitly; a backend
should provide a native endpoint ID before the Settings flow permits choosing
one of them.

`MicrophoneInventory.resolve(saved_id)` returns an explicit
`MicrophoneSelectionState`:

- `SELECTED` means the saved endpoint is present and has an input channel.
- `DEFAULT` means no saved endpoint was requested and the current default is
  usable.
- `FALLBACK_DEFAULT` means the saved endpoint is stale/unusable and the
  current default is being used. The reason remains available to the caller so
  Settings can show a visible fallback warning.
- `UNAVAILABLE` means no safe input endpoint is known. The resolver never
  chooses an arbitrary non-default device in this state.

`MicrophoneSettings` is a small versioned mapping boundary. It stores only the
selected identity and supports the legacy `microphone_id` key for migration.
The repository/UI integration that persists this mapping in `config.json`,
refreshes it on hot-plug, and exposes an input-level test is intentionally a
follow-up.

## Recording boundaries

`RecordingControls` is versioned and behavior-preserving by default:

```python
RecordingControls(
    max_duration_seconds=None,  # no new hard stop by default
    vad=VADSettings(enabled=False),  # no aggressive silence detection
)
```

`MaximumDurationPolicy` is a pure elapsed-time function. Once a caller opts
into a maximum, it reports a warning window and a deterministic
`MAX_DURATION` terminal reason at or beyond the limit.

`SilenceVADPolicy` accepts explicit monotonic timestamps and normalized input
levels. It requires observed speech for `minimum_speech_seconds`, followed by
continuous below-threshold input for `silence_duration_seconds`; silence
without speech never stops a session. A timestamp regression is rejected so a
wall-clock adjustment cannot terminate a recording early.
`RecordingBoundaryPolicy` combines both policies and exposes explicit
`ACTIVE`, `STOPPED`, `CANCELLED`, and `DEVICE_UNAVAILABLE` states without
touching an audio stream or publishing text.

An audio adapter should call the policy before requesting
`RecordingSession.stop()`. It must preserve the existing owner/cancellation
order: cancellation wins over publication, snapshots are taken before
provider work, and temporary WAV cleanup remains owned by `RecordingSession`.
A future implementation must also decide how a short/empty capture is
surfaced rather than treating a policy decision as proof that usable audio
exists.

## Deferred product acceptance

The following remain outside this incremental foundation and are required
before issue #52 can close:

- Settings UI for listing, selecting, testing, and persisting microphones;
- visible missing-device and stale-selection messages;
- real Windows PortAudio/SoX hot-plug and device-loss behavior;
- maximum-duration warning presentation;
- start/stop audio cues and documentation of media-playback pause behavior;
- packaged Windows acceptance with fake inventory/unit evidence supplemented by
  manual hot-plug checks.

No microphone names, input levels, recordings, or transcript text are sent to a
provider or usage-statistics path by this module.
