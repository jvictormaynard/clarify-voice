# Local transcription history boundary

This document describes the local history boundary and its current desktop
integration for issue #53. The Settings window now exposes a reversible,
disabled-by-default toggle, retention policy, refresh, export, and delete-all
actions. Successful workflow results and terminal error summaries are written
only after the workflow publishes its terminal state. This remains a `Part of
#53` implementation: packaged Windows acceptance is still a separate manual
gate.

## Privacy contract

`HistoryStore` is disabled unless the user explicitly enables **Local history**
in Settings. A disabled store does not read the history path, create a
directory, or retain transcript text in memory. `delete_all()` remains
available while disabled so turning the feature off does not prevent a user
from erasing an existing history immediately.

Each `HistoryRecord` contains only:

- raw transcript text (optional for partial/error records);
- refined output (optional);
- workflow, UTC timestamp, transcription provider/model, optional refinement
  provider/model, status, and a concise error summary.

The type has no audio path/bytes, API-key field, request headers, provider
payload, or telemetry metadata. Unknown fields from a legacy document are
dropped during migration and export. Error strings are guarded against common
`api_key`, bearer-token, secret, and password patterns, but callers should
still provide a short safe error summary rather than a provider response.

## Persistence and recovery

The first version uses a small versioned JSON document (`schema_version: 1`)
instead of adding SQLite. History has no search, sharing, synchronization, or
high-volume queue requirement yet; a single document keeps migration,
delete-all, and export behavior auditable and avoids another runtime
dependency. A future issue can move the boundary to SQLite without changing
the typed record contract if indexing or volume justifies it.

Writes serialize only canonical record fields to a same-directory temporary
file, flush and `fsync` it, and atomically replace the primary file. If startup
finds the primary file missing or unreadable, it can recover the newest intact
temporary JSON snapshot left by an interrupted replacement. When both files
are valid, a newer temporary snapshot (by filesystem mtime) is recovered and
an older leftover is discarded; the committed primary wins when there is no
evidence that the temp was newer. Malformed temporary files are removed. An
operation that crashed before replacement is therefore recovered when
possible, but it cannot corrupt the previous history. A valid temporary file
with a schema newer than this executable is preserved for a future executable
instead of being deleted.

Schema version 0 prototype shapes (`history`, `entries`, or `records`) migrate
to version 1. Malformed entries are skipped while valid records remain
available, and the repaired canonical document is written atomically. A newer
schema is refused rather than overwritten by an older executable.

If the primary JSON is malformed or unreadable and no intact temporary
snapshot can be recovered, reads and appends fail closed with a typed error.
The corrupt bytes are left in place for diagnosis or manual recovery rather
than being silently replaced by a new snapshot.

## Retention, deletion, and export

`retention_days` defaults to 30 for an enabled store. Pass another
non-negative integer to expire records at startup and before each append, or
pass `None` for an explicit no-expiry policy. Expired records are removed from
the same atomic snapshot; retention never touches audio because this boundary
does not retain audio.

`delete_all()` removes the primary snapshot and any interrupted-write files.
`export(destination, format=...)` writes retained records atomically as:

- `txt`: readable plain text with metadata and raw/refined/error sections;
- `markdown` (or `md`): headings and fenced Unicode/multiline text blocks;
- `json`: the same versioned, canonical record shape as the persistence file.

The export destination must differ from the history file. Export is unavailable
while the store is disabled, and neither export nor persistence includes
telemetry or provider credentials.

The desktop page offers separate **Copy source** and **Copy result** actions
when the corresponding field exists. It deliberately does not offer a live
retry action yet: the current workflow contract does not retain audio or a
focus-safe source target after completion, so silently replaying a record would
be unsafe. The page labels this boundary rather than pretending that retry is
available.

Prompt-mode dictation through a non-multimodal transcription provider keeps
the raw provider transcript in `raw_text`, the second-route output in
`refined_text`, and both route identifiers in the metadata. Multimodal and
plain transcription paths leave the refined field empty rather than labeling
the final text as a separate refinement.

The normal profile stores `history.json` beside `config.json`. Injected
repository bundles use that same profile-relative rule only when their config
adapter exposes a local `path`; a pathless adapter must set
`ApplicationRepositories.history_path` explicitly, otherwise startup fails
closed instead of writing transcripts to the production profile.

## Follow-up needed for #53

Packaged Windows acceptance remains: verify the installed Settings path,
restart recovery, retention/delete-all, export destinations, and that usage
statistics contain no transcript text. A future issue can add safe retry only
after the workflow layer defines an explicit retained source contract.
