# Local dictionary and snippets

ClarifyVoice keeps user vocabulary and snippets in a non-secret profile file:

- Windows: `%APPDATA%\ClarifyVoice\dictionary.json`
- Linux/macOS development runs: `~/.clarifyvoice/dictionary.json`

The file is independent from `config.json` and the provider secret store.  It
contains no API credentials and is never added to usage statistics.  Writes are
validated, flushed, and atomically replaced, so a process interruption cannot
leave a partially written document in place.

## Versioned format

The current format is schema version `1`:

```json
{
  "schema_version": 1,
  "dictionary": [
    {
      "term": "OpenWhispr",
      "pronunciation": "open whisper",
      "aliases": ["Open Whisper"],
      "enabled": true
    }
  ],
  "snippets": [
    {
      "trigger": ";meet",
      "replacement": "Agenda:\n1. Confirm scope\n2. Share notes",
      "enabled": true,
      "case_sensitive": false
    }
  ]
}
```

The first unversioned document (`schema_version` omitted) is migrated in
memory to version 1 when loaded.  Unknown future versions are rejected for
import and are never overwritten by a save.  Entries are bounded to 512
dictionary entries and 512 snippets.  Terms, aliases, pronunciation, and
triggers are single-line Unicode strings with a 256-character limit;
replacements may contain Unicode and newlines up to 4096 characters.  Empty
replacements are allowed when a trigger should be removed.

`LocalDictionarySnippetsRepository.import_json()` parses and validates the
complete document before replacing the active file.  Invalid JSON, duplicate
terms/triggers, wrong field types, oversized values, and future schema
versions therefore leave the previous profile unchanged.  `export_json()` and
`export_to()` emit the canonical, human-readable document while preserving
entry ordering.

## Matching behavior

`DictionarySnippetService.expand()` scans left-to-right and only considers
enabled snippets.  Input and triggers use canonical Unicode NFC matching, so
precomposed and decomposed forms (for example `café` and `cafe\u0301`) behave
the same while the original combining sequence is consumed as one unit.  NFC
composition boundaries are derived from the active Unicode database, rather
than a script-specific list, so starter-to-starter scripts remain covered as
the runtime's Unicode version evolves.  A trigger must be separated from
adjacent Unicode letters/numbers and `_`;
punctuation at either side is allowed.  Matching is case-insensitive by
default, or exact when `case_sensitive` is true.  When triggers overlap, the
longest trigger wins and equal-length triggers retain the order in the file.
The input is bounded to one million characters by default and matching walks a
prefix trie instead of checking every rule at every character.  The result is
produced without executing replacement text as code; expanded output is
subject to the same configured character bound.

Enabled dictionary entries produce an optional, provider-neutral transcription
context.  It is bounded to 4096 characters and keeps the stored order.  Cloud
transcription adapters forward that context through the typed
`TranscriptionRequest`; local/offline adapters may ignore it.  The HTTP
diagnostics boundary logs only provider metadata and redacts request bodies,
so dictionary content and credentials are not written to provider logs.

## Settings workflow

The Settings window exposes the local profile under **Dictionary**.  It can
search terms, aliases, pronunciation metadata, snippet triggers, and
replacement text; add or edit either item; enable/disable entries; and delete
individual rows.  The **Reset all** action clears both collections only after
confirmation.  Every change is validated and atomically persisted before the
UI reports success.

The same page includes a snippet preview, plus JSON **Import** and **Export**
actions.  Import uses the all-or-nothing validation boundary described above,
so malformed or future-schema documents cannot replace the active profile.
The profile remains local and is excluded from usage statistics and provider
diagnostics.  Windows manual acceptance of the complete workflow is still a
release-check item; it does not change the documented file format or runtime
guarantees.
