# Microphone selection and recording controls (issue #52)

The application connects the UI-independent microphone policies to the typed
config repository, the Settings surface, and the existing
`Recorder`/`RecordingSession` owner. Device selection is resolved from a fresh
PortAudio inventory before every session; recording boundaries are observed by
the audio callback and by a small policy worker, so hard duration limits also
work when capture is SoX-only and the optional PortAudio meter is unavailable.
The owning workflow is stopped through a lifecycle worker. Cancellation and
temporary-WAV cleanup remain owned by `RecordingSession`.

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
`AppConfig` persists this mapping under `microphone`; the Settings page refreshes
the inventory, shows a visible stale-selection fallback, and runs a short
input-level test without writing or transmitting audio. A recording resolves
the same selection again at start, so a hot-plug change cannot silently reuse a
stale backend index.

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
publishing text. The recorder calls the combined policy from its level
callback, while a short-lived policy worker observes only the opted-in hard
duration limit. Keeping the worker's duration clock separate prevents it from
feeding synthetic levels into VAD or racing real callbacks; both paths signal
the same boundary event. The worker is stopped and joined with the recorder
process during explicit stop/cancel, so it cannot keep a session alive after
cleanup. `RecordingSession` invokes the workflow stop
callback from a separate lifecycle worker, never from the PortAudio callback
itself. The existing owner/cancellation order remains in force:
cancellation wins over publication, snapshots are taken before provider work,
and temporary WAV cleanup remains owned by `RecordingSession`. A policy
decision is not treated as proof that usable audio exists; the normal WAV
size/encoding gate still returns `no_audio` for an empty capture.

## Deferred product acceptance

The following remain outside this incremental integration and are required
before issue #52 can close:

- real Windows PortAudio/SoX hot-plug and device-loss behavior;
- visual polish for maximum-duration warning presentation;
- start/stop audio cues and documentation of media-playback pause behavior;
- packaged Windows acceptance with fake inventory/unit evidence supplemented by
  manual hot-plug checks.

No microphone names, input levels, recordings, or transcript text are sent to a
provider or usage-statistics path by this module.
