"""Local dictionary and snippet primitives.

The dictionary is deliberately a small, non-secret profile artifact.  It is
kept separate from provider credentials (``config.json`` and the secret store)
and is written with the same replace-after-fsync pattern used by the other
local repositories.  The module has no UI or provider-specific branches; the
application can opt into the generated transcription context and run the
bounded snippet expander after a result is received.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import tempfile
import threading
import unicodedata
from typing import Any, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from provider_types import TranscriptionRequest


DICTIONARY_SCHEMA_VERSION = 1

# These limits are intentionally conservative.  They protect startup,
# matching, and prompt construction from a damaged or hand-edited profile.
MAX_DICTIONARY_ENTRIES = 512
MAX_SNIPPETS = 512
MAX_TERM_LENGTH = 256
MAX_ALIAS_LENGTH = 256
MAX_PRONUNCIATION_LENGTH = 256
MAX_TRIGGER_LENGTH = 256
MAX_REPLACEMENT_LENGTH = 4096
MAX_CONTEXT_CHARS = 4096
MAX_EXPANSION_CHARS = 1_000_000


class DictionarySnippetsError(ValueError):
    """Raised when a dictionary/snippets document is invalid."""


class UnsupportedDictionarySchemaError(OSError):
    """Raised when a save would downgrade a newer profile document."""


def _text(value: Any, field: str, *, maximum: int,
          allow_newlines: bool = False, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise DictionarySnippetsError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFC", value)
    if not allow_empty and (not normalized or not normalized.strip()):
        raise DictionarySnippetsError(f"{field} must not be empty")
    if len(normalized) > maximum:
        raise DictionarySnippetsError(
            f"{field} exceeds the {maximum}-character limit")
    if not allow_newlines and any(char in normalized for char in "\r\n"):
        raise DictionarySnippetsError(f"{field} must be a single line")
    return normalized


def _optional_text(value: Any, field: str, *, maximum: int,
                   allow_newlines: bool = False) -> str:
    if value is None or value == "":
        return ""
    return _text(value, field, maximum=maximum, allow_newlines=allow_newlines)


def _enabled(value: Any, field: str = "enabled") -> bool:
    if not isinstance(value, bool):
        raise DictionarySnippetsError(f"{field} must be a boolean")
    return value


def _aliases(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise DictionarySnippetsError("aliases must be a list of strings")
    if len(value) > 32:
        raise DictionarySnippetsError("aliases exceed the 32-item limit")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        alias = _text(item, "alias", maximum=MAX_ALIAS_LENGTH)
        key = alias.casefold()
        if key in seen:
            raise DictionarySnippetsError("aliases must be unique")
        seen.add(key)
        result.append(alias)
    return tuple(result)


@dataclass(frozen=True)
class DictionaryEntry:
    """One preferred vocabulary term and optional metadata.

    ``pronunciation`` and ``aliases`` are context metadata.  They are not
    persisted in usage statistics and are never treated as executable input.
    """

    term: str
    pronunciation: str = ""
    aliases: tuple[str, ...] = ()
    enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "term", _text(
            self.term, "term", maximum=MAX_TERM_LENGTH))
        object.__setattr__(self, "pronunciation", _optional_text(
            self.pronunciation, "pronunciation",
            maximum=MAX_PRONUNCIATION_LENGTH))
        object.__setattr__(self, "aliases", _aliases(self.aliases))
        object.__setattr__(self, "enabled", _enabled(self.enabled))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DictionaryEntry":
        if not isinstance(value, Mapping):
            raise DictionarySnippetsError("dictionary entry must be an object")
        return cls(
            term=value.get("term", ""),
            pronunciation=value.get("pronunciation", ""),
            aliases=value.get("aliases", ()),
            enabled=value.get("enabled", True),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "pronunciation": self.pronunciation,
            "aliases": list(self.aliases),
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class Snippet:
    """A deterministic, bounded text replacement rule."""

    trigger: str
    replacement: str
    enabled: bool = True
    case_sensitive: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "trigger", _text(
            self.trigger, "trigger", maximum=MAX_TRIGGER_LENGTH))
        object.__setattr__(self, "replacement", _text(
            self.replacement, "replacement", maximum=MAX_REPLACEMENT_LENGTH,
            allow_newlines=True, allow_empty=True))
        object.__setattr__(self, "enabled", _enabled(self.enabled))
        if not isinstance(self.case_sensitive, bool):
            raise DictionarySnippetsError("case_sensitive must be a boolean")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Snippet":
        if not isinstance(value, Mapping):
            raise DictionarySnippetsError("snippet must be an object")
        return cls(
            trigger=value.get("trigger", ""),
            replacement=value.get("replacement", ""),
            enabled=value.get("enabled", True),
            case_sensitive=value.get("case_sensitive", False),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger,
            "replacement": self.replacement,
            "enabled": self.enabled,
            "case_sensitive": self.case_sensitive,
        }


@dataclass(frozen=True)
class DictionarySnippets:
    """Validated profile state, preserving list ordering for stable output."""

    dictionary: tuple[DictionaryEntry, ...] = ()
    snippets: tuple[Snippet, ...] = ()
    schema_version: int = DICTIONARY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DICTIONARY_SCHEMA_VERSION:
            raise DictionarySnippetsError(
                f"unsupported dictionary schema {self.schema_version}")
        if not isinstance(self.dictionary, tuple):
            object.__setattr__(self, "dictionary", tuple(self.dictionary))
        if not isinstance(self.snippets, tuple):
            object.__setattr__(self, "snippets", tuple(self.snippets))
        if len(self.dictionary) > MAX_DICTIONARY_ENTRIES:
            raise DictionarySnippetsError(
                f"dictionary exceeds {MAX_DICTIONARY_ENTRIES} entries")
        if len(self.snippets) > MAX_SNIPPETS:
            raise DictionarySnippetsError(
                f"snippets exceed {MAX_SNIPPETS} entries")
        if any(not isinstance(entry, DictionaryEntry)
               for entry in self.dictionary):
            raise DictionarySnippetsError("dictionary contains an invalid entry")
        if any(not isinstance(snippet, Snippet) for snippet in self.snippets):
            raise DictionarySnippetsError("snippets contains an invalid entry")
        dictionary_keys = [entry.term.casefold() for entry in self.dictionary]
        if len(dictionary_keys) != len(set(dictionary_keys)):
            raise DictionarySnippetsError("dictionary terms must be unique")
        snippet_keys = [snippet.trigger.casefold() for snippet in self.snippets]
        if len(snippet_keys) != len(set(snippet_keys)):
            raise DictionarySnippetsError("snippet triggers must be unique")

    @classmethod
    def empty(cls) -> "DictionarySnippets":
        return cls()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "DictionarySnippets":
        """Parse the versioned document without partially accepting it."""
        if not isinstance(value, Mapping):
            raise DictionarySnippetsError("dictionary document must be an object")
        migrated = _migrate_payload(value)
        dictionary_payload = migrated.get("dictionary", [])
        snippets_payload = migrated.get("snippets", [])
        if not isinstance(dictionary_payload, list):
            raise DictionarySnippetsError("dictionary must be a list")
        if not isinstance(snippets_payload, list):
            raise DictionarySnippetsError("snippets must be a list")
        entries = tuple(DictionaryEntry.from_mapping(item)
                       for item in dictionary_payload)
        snippets = tuple(Snippet.from_mapping(item)
                         for item in snippets_payload)
        return cls(entries, snippets)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": DICTIONARY_SCHEMA_VERSION,
            "dictionary": [entry.to_mapping() for entry in self.dictionary],
            "snippets": [snippet.to_mapping() for snippet in self.snippets],
        }


def _schema_version(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _migrate_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """Migrate the initial unversioned draft format to schema v1.

    Version zero had the same fields but no explicit version.  Keeping this
    migration makes hand-created early profiles recoverable while still
    rejecting every future version that this build cannot safely write.
    """
    raw_version = value.get("schema_version")
    if raw_version is not None and (
            not isinstance(raw_version, int) or isinstance(raw_version, bool)):
        raise DictionarySnippetsError("schema_version must be an integer")
    version = _schema_version(raw_version)
    if version > DICTIONARY_SCHEMA_VERSION:
        raise DictionarySnippetsError(
            f"unsupported dictionary schema {version}")
    migrated = dict(value)
    if version == 0:
        migrated["schema_version"] = DICTIONARY_SCHEMA_VERSION
    return migrated


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


class LocalDictionarySnippetsRepository:
    """Atomic JSON-backed profile storage for non-secret dictionary data."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self) -> DictionarySnippets:
        with self._lock:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                return DictionarySnippets.from_mapping(payload)
            except (OSError, TypeError, ValueError, json.JSONDecodeError,
                    DictionarySnippetsError):
                # Startup should remain available when a hand-edited profile
                # is malformed.  The invalid file is left untouched so the
                # user can recover it; a later import/save replaces it
                # atomically only after complete validation.
                return DictionarySnippets.empty()

    def save(self, state: DictionarySnippets | Mapping[str, Any]) -> None:
        with self._lock:
            current_schema = self._current_schema_version()
            if current_schema > DICTIONARY_SCHEMA_VERSION:
                raise UnsupportedDictionarySchemaError(
                    f"Cannot save dictionary schema {current_schema} with "
                    f"supported schema {DICTIONARY_SCHEMA_VERSION}")
            validated = (state if isinstance(state, DictionarySnippets)
                         else DictionarySnippets.from_mapping(state))
            _atomic_write_json(self.path, validated.to_mapping())

    def export_json(self) -> str:
        with self._lock:
            return json.dumps(
                self.load().to_mapping(),
                ensure_ascii=False,
                indent=2,
            ) + "\n"

    def export_to(self, destination: str | os.PathLike[str]) -> Path:
        """Write a validated export without changing the active profile."""
        destination_path = Path(destination)
        with self._lock:
            _atomic_write_json(destination_path, self.load().to_mapping())
        return destination_path

    def import_json(self, document: str | bytes | Mapping[str, Any]) -> DictionarySnippets:
        """Validate and replace the profile in one atomic operation.

        Parsing happens before ``save``.  Therefore malformed JSON, invalid
        fields, duplicate triggers, and unsupported versions cannot mutate the
        previous state.
        """
        if isinstance(document, bytes):
            try:
                document = document.decode("utf-8")
            except UnicodeDecodeError as error:
                raise DictionarySnippetsError("invalid dictionary UTF-8") from error
        if isinstance(document, str):
            try:
                payload = json.loads(document)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise DictionarySnippetsError("invalid dictionary JSON") from error
        else:
            payload = document
        validated = DictionarySnippets.from_mapping(payload)
        self.save(validated)
        return validated

    def _current_schema_version(self) -> int:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return 0
        if not isinstance(payload, Mapping):
            return 0
        return _schema_version(payload.get("schema_version"))


def _is_word_character(value: str) -> bool:
    """Return the boundary class used by snippet matching.

    ``str.isalnum`` handles Unicode letters and numbers.  Underscore is kept
    in the word class so ``foo`` does not expand inside ``foo_bar``.
    """
    return bool(value) and (value == "_" or value.isalnum())


def _has_word_boundaries(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""
    return not _is_word_character(before) and not _is_word_character(after)


def _snippet_sort_key(item: tuple[int, Snippet]) -> tuple[int, int]:
    # Longest first handles overlapping triggers.  Original list order is the
    # stable tie-breaker, making imported/exported documents deterministic.
    index, snippet = item
    return (-len(snippet.trigger.casefold()), index)


def _casefold_match_end(text: str, start: int, trigger: str) -> int | None:
    """Find the original-text end for a casefolded trigger.

    Unicode case folding is not one code point in, one code point out:
    ``"ß".casefold() == "ss"``.  Building the folded prefix incrementally
    keeps the consumed span correct without using a lossy regex transform.
    """
    target = trigger.casefold()
    folded = ""
    end = start
    while end < len(text) and len(folded) <= len(target):
        piece = text[end].casefold()
        folded += piece
        end += 1
        if folded == target:
            return end
        if not target.startswith(folded):
            return None
    return None


class DictionarySnippetService:
    """Thread-safe in-memory view over a local dictionary/snippet repository."""

    def __init__(self, repository: LocalDictionarySnippetsRepository,
                 *, max_expansion_chars: int = MAX_EXPANSION_CHARS) -> None:
        if not 1 <= max_expansion_chars <= MAX_EXPANSION_CHARS:
            raise ValueError("max_expansion_chars is outside the supported range")
        self.repository = repository
        self.max_expansion_chars = max_expansion_chars
        self._lock = threading.RLock()
        self._state = repository.load()

    @property
    def state(self) -> DictionarySnippets:
        with self._lock:
            return copy.deepcopy(self._state)

    def reload(self) -> DictionarySnippets:
        with self._lock:
            self._state = self.repository.load()
            return self.state

    def replace(self, state: DictionarySnippets | Mapping[str, Any]) -> DictionarySnippets:
        validated = (state if isinstance(state, DictionarySnippets)
                     else DictionarySnippets.from_mapping(state))
        with self._lock:
            self.repository.save(validated)
            self._state = validated
            return self.state

    def import_json(self, document: str | bytes | Mapping[str, Any]) -> DictionarySnippets:
        if isinstance(document, bytes):
            try:
                document = document.decode("utf-8")
            except UnicodeDecodeError as error:
                raise DictionarySnippetsError("invalid dictionary UTF-8") from error
        if isinstance(document, str):
            try:
                payload = json.loads(document)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise DictionarySnippetsError("invalid dictionary JSON") from error
        else:
            payload = document
        validated = DictionarySnippets.from_mapping(payload)
        with self._lock:
            self.repository.save(validated)
            self._state = validated
            return self.state

    def export_json(self) -> str:
        with self._lock:
            return json.dumps(
                self._state.to_mapping(), ensure_ascii=False, indent=2) + "\n"

    def expand(self, text: str) -> str:
        """Expand enabled snippets with Unicode word-boundary matching.

        Matching is left-to-right.  At one position the longest trigger wins;
        equal-length rules retain document order.  A disabled snippet is
        ignored, and malformed state cannot reach this method because the
        repository validates before replacement.
        """
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if len(text) > self.max_expansion_chars:
            raise DictionarySnippetsError(
                f"text exceeds the {self.max_expansion_chars}-character limit")
        with self._lock:
            candidates = sorted(
                ((index, snippet) for index, snippet in enumerate(self._state.snippets)
                 if snippet.enabled),
                key=_snippet_sort_key,
            )
        if not candidates or not text:
            return text

        output: list[str] = []
        output_length = 0
        index = 0
        while index < len(text):
            replacement: str | None = None
            consumed = 0
            for _rule_index, snippet in candidates:
                if snippet.case_sensitive:
                    end = index + len(snippet.trigger)
                    matches = (
                        end <= len(text)
                        and _has_word_boundaries(text, index, end)
                        and text[index:end] == snippet.trigger
                    )
                else:
                    end = _casefold_match_end(text, index, snippet.trigger)
                    matches = (
                        end is not None
                        and _has_word_boundaries(text, index, end)
                    )
                if matches:
                    replacement = snippet.replacement
                    consumed = end - index
                    break
            if replacement is None:
                if output_length + 1 > self.max_expansion_chars:
                    raise DictionarySnippetsError(
                        "expanded text exceeds the configured character limit")
                output.append(text[index])
                output_length += 1
                index += 1
            else:
                if output_length + len(replacement) > self.max_expansion_chars:
                    raise DictionarySnippetsError(
                        "expanded text exceeds the configured character limit")
                output.append(replacement)
                output_length += len(replacement)
                index += consumed
        return "".join(output)

    def transcription_context(self, *, max_chars: int = MAX_CONTEXT_CHARS) -> str:
        """Build bounded provider-neutral vocabulary context.

        The returned string is intended for a typed transcription request.  It
        is never written to usage statistics or diagnostics by this module.
        Entries are ordered as stored, and truncation occurs only between
        entries so a term is never emitted partially.
        """
        if not 1 <= max_chars <= MAX_CONTEXT_CHARS:
            raise ValueError("max_chars is outside the supported range")
        with self._lock:
            entries = tuple(entry for entry in self._state.dictionary
                            if entry.enabled)
        if not entries:
            return ""
        lines = [
            "Use the following user-provided vocabulary when transcribing. "
            "Preserve these terms when they fit the audio:",
        ]
        if len(lines[0]) > max_chars:
            return ""
        for entry in entries:
            detail = entry.term
            if entry.aliases:
                detail += " (aliases: " + ", ".join(entry.aliases) + ")"
            if entry.pronunciation:
                detail += " (pronunciation: " + entry.pronunciation + ")"
            line = f"- {detail}"
            candidate = "\n".join((*lines, line))
            if len(candidate) > max_chars:
                break
            lines.append(line)
        if len(lines) == 1:
            return ""
        return "\n".join(lines)

    def apply_context(self, request: "TranscriptionRequest", *,
                      max_chars: int = MAX_CONTEXT_CHARS) -> "TranscriptionRequest":
        """Return a request carrying optional context without provider branches."""
        context = self.transcription_context(max_chars=max_chars)
        if not context:
            return request
        return replace(request, dictionary_context=context)
