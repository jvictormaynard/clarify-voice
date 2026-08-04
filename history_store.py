"""Opt-in, local-only transcription history storage.

The application intentionally has no transcript-history dependency in its
runtime path yet.  This module is the UI-independent persistence boundary for
the future history setting: callers have to construct ``HistoryStore`` with
``enabled=True`` before a transcript can be written or read.

History is a small, privacy-sensitive dataset, so the first version uses a
versioned JSON document rather than adding a database dependency.  Writes are
made to a same-directory temporary file, flushed, and atomically replaced.
An intact temporary document can be recovered when startup finds the primary
file missing or interrupted, while malformed temporary files are discarded.
An unrecoverable corrupt primary is surfaced as a typed error and is never
silently overwritten.
No audio, API keys, provider payloads, or telemetry fields are accepted by the
typed record and they are not included in any export format.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping


HISTORY_SCHEMA_VERSION = 1
DEFAULT_RETENTION_DAYS = 30
HistoryStatus = Literal["success", "partial", "error", "cancelled"]
HISTORY_STATUSES: frozenset[str] = frozenset({
    "success",
    "partial",
    "error",
    "cancelled",
})


class HistoryStoreError(OSError):
    """Base error for an unavailable or invalid history store."""


class HistoryDisabledError(HistoryStoreError):
    """Raised when a disabled store is asked to read or export history."""


class UnsupportedHistorySchemaVersionError(HistoryStoreError):
    """Raised when a newer history file could be damaged by this version."""


class HistoryValidationError(ValueError):
    """Raised when a record cannot be represented by the history contract."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_record_id() -> str:
    return uuid.uuid4().hex


def _coerce_timestamp(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise HistoryValidationError("timestamp must not be empty")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as error:
            raise HistoryValidationError("timestamp must be ISO-8601") from error
    else:
        raise HistoryValidationError("timestamp must be a datetime or ISO-8601")

    # A naive timestamp is accepted for source compatibility, but is treated
    # as local data in UTC rather than silently applying the host timezone.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z")


_SENSITIVE_AUTH_PATTERN = re.compile(
    r"(?is)(['\"]?(?:authorization|bearer)['\"]?\s*[:=]\s*"
    r"(?:[a-z]+\s+)?)(?:(['\"])((?:\\.|(?!\2).)*?)\2|"
    r"([^,;&}\n]+))")
_SENSITIVE_FIELD_PATTERN = re.compile(
    r"(?is)(['\"]?(?:api[_ -]?key|access[_ -]?token|"
    r"client[_ -]?secret|credential|password|secret|token)['\"]?\s*[:=]\s*)"
    r"(?:(['\"])((?:\\.|(?!\2).)*?)\2|([^,;&}\n]+))")
# Some providers serialize an error body inside another string and escape the
# mapping's quotes (for example, ``body="{\"password\":\"secret\"}"``).
# The ordinary field matcher cannot see the key/value delimiters in that form.
# Keep the escapes intact while replacing the value so the resulting error
# remains readable and, more importantly, never persists the credential.
_SENSITIVE_ESCAPED_FIELD_PREFIX_PATTERN = re.compile(
    r"(?is)(\\?['\"]?(?:authorization|bearer|api[_ -]?key|access[_ -]?token|"
    r"client[_ -]?secret|credential|password|secret|token)"
    r"\\?['\"]?\s*[:=]\s*(?:[a-z]+\s+)?)"
    r"((?:\\)?['\"])")
_SENSITIVE_BEARER_PATTERN = re.compile(r"(?i)(bearer)\s+([^\s,;&]+)")
_SENSITIVE_QUERY_PATTERN = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|token)=)[^&#\s]+")


def _redact_field_match(match: re.Match[str]) -> str:
    prefix = match.group(1)
    quote = match.group(2)
    if quote is not None:
        return f"{prefix}{quote}<redacted>{quote}"
    return f"{prefix}<redacted>"


def _escaped_field_value_end(
    value: str,
    start: int,
    quote: str,
) -> int | None:
    """Return the end of a quoted value using a linear escape scanner.

    A doubly serialized quote delimiter is represented by ``\\"``.  A quote
    escaped inside the nested value has three (or more) preceding backslashes,
    while a literal trailing backslash adds four escapes per nesting level.
    Consequently, the delimiter is the quote preceded by a backslash run
    congruent to 1 modulo 4.  Plain quoted values use the usual even/odd
    backslash rule.  The scanner deliberately returns ``None`` for truncated
    values rather than retrying an overlapping regular expression.
    """

    quote_character = quote[-1]
    serialized_delimiter = quote.startswith("\\")
    backslash_run = 0
    for index in range(start, len(value)):
        character = value[index]
        if character == "\\":
            backslash_run += 1
            continue
        if character == quote_character:
            if serialized_delimiter:
                if backslash_run % 4 == 1:
                    return index + 1
            elif backslash_run % 2 == 0:
                return index + 1
        backslash_run = 0
    return None


def _redact_escaped_fields(value: str) -> str:
    """Redact sensitive values in escaped/doubly serialized mappings.

    Only the bounded key/prefix is matched by regex; the value is consumed by
    the linear scanner above.  This keeps malformed provider error strings
    from triggering catastrophic backtracking while preserving the original
    escaped representation for valid values.
    """

    pieces: list[str] = []
    cursor = 0
    search_at = 0
    while True:
        match = _SENSITIVE_ESCAPED_FIELD_PREFIX_PATTERN.search(value, search_at)
        if match is None:
            pieces.append(value[cursor:])
            return "".join(pieces)

        pieces.append(value[cursor:match.start()])
        quote = match.group(2)
        end = _escaped_field_value_end(value, match.end(), quote)
        replacement = f"{match.group(1)}{quote}<redacted>"
        if end is None:
            # A truncated credential is still sensitive; redact through EOF
            # instead of preserving the untrusted tail or retrying a regex.
            pieces.append(replacement)
            return "".join(pieces)

        pieces.append(f"{replacement}{quote}")
        cursor = end
        search_at = end


def _safe_error(value: str | None) -> str | None:
    """Keep concise error metadata without persisting obvious credentials."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise HistoryValidationError("error must be text or None")
    if not value:
        return None
    sanitized = value
    sanitized = _redact_escaped_fields(sanitized)
    sanitized = _SENSITIVE_AUTH_PATTERN.sub(_redact_field_match, sanitized)
    sanitized = _SENSITIVE_FIELD_PATTERN.sub(_redact_field_match, sanitized)
    sanitized = _SENSITIVE_BEARER_PATTERN.sub(r"\1 <redacted>", sanitized)
    sanitized = _SENSITIVE_QUERY_PATTERN.sub(r"\1<redacted>", sanitized)
    return sanitized


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    """A single raw/refined result and safe provider metadata.

    ``raw_text`` and ``refined_text`` may be absent for partial, failed, or
    cancelled operations.  ``provider`` and ``model`` are identifiers only;
    API credentials and complete provider responses are deliberately not part
    of this type.
    """

    raw_text: str | None = None
    refined_text: str | None = None
    workflow: str = "transcription"
    timestamp: datetime = field(default_factory=_utc_now)
    provider: str = "unknown"
    model: str = "unknown"
    status: HistoryStatus = "success"
    error: str | None = None
    record_id: str = field(default_factory=_new_record_id)

    def __post_init__(self) -> None:
        for field_name in ("raw_text", "refined_text"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise HistoryValidationError(f"{field_name} must be text or None")

        if not isinstance(self.workflow, str) or not self.workflow.strip():
            raise HistoryValidationError("workflow must be non-empty text")
        object.__setattr__(self, "workflow", self.workflow.strip())

        for field_name in ("provider", "model"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise HistoryValidationError(f"{field_name} must be text")
            object.__setattr__(
                self, field_name, value.strip() or "unknown")

        value = self.record_id
        if not isinstance(value, str) or not value.strip():
            raise HistoryValidationError("record_id must be non-empty text")
        object.__setattr__(self, "record_id", value.strip())

        timestamp = _coerce_timestamp(self.timestamp)
        object.__setattr__(self, "timestamp", timestamp)

        if not isinstance(self.status, str):
            raise HistoryValidationError("status must be text")
        status = self.status.strip().lower()
        if status not in HISTORY_STATUSES:
            raise HistoryValidationError(
                f"status must be one of {sorted(HISTORY_STATUSES)}")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "error", _safe_error(self.error))

    @property
    def id(self) -> str:
        """Compatibility alias for callers that call the key simply ``id``."""

        return self.record_id

    def to_mapping(self) -> dict[str, Any]:
        """Return only the versioned, privacy-safe persistence fields."""

        return {
            "id": self.record_id,
            "raw_text": self.raw_text,
            "refined_text": self.refined_text,
            "workflow": self.workflow,
            "timestamp": _format_timestamp(self.timestamp),
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "error": self.error,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "HistoryRecord":
        """Parse a record while ignoring fields outside the safe contract."""

        if not isinstance(payload, Mapping):
            raise HistoryValidationError("history record must be an object")

        record_id = payload.get("id", payload.get("record_id", _new_record_id()))
        raw_text = payload.get(
            "raw_text", payload.get("raw", payload.get("text")))
        refined_text = payload.get("refined_text", payload.get("refined"))
        workflow = payload.get("workflow", payload.get("mode", "transcription"))
        timestamp = payload.get("timestamp", payload.get("time", _utc_now()))
        provider = payload.get("provider", "")
        model = payload.get("model", "")
        status = payload.get("status", "success")
        error = payload.get("error")

        # Legacy payloads may omit provider/model.  Empty identifiers are
        # normalized to an explicit local/unknown marker, never inferred from
        # an API key or full provider payload.
        if not isinstance(provider, str) or not provider.strip():
            provider = "unknown"
        if not isinstance(model, str) or not model.strip():
            model = "unknown"
        return cls(
            raw_text=raw_text,
            refined_text=refined_text,
            workflow=workflow,
            timestamp=timestamp,
            provider=provider,
            model=model,
            status=status,
            error=error,
            record_id=record_id,
        )


_HISTORY_RECORD_FIELDS: tuple[str, ...] = (
    "id",
    "raw_text",
    "refined_text",
    "workflow",
    "timestamp",
    "provider",
    "model",
    "status",
    "error",
)


def _version(value: Any) -> int | None:
    """Parse an explicit schema marker, rejecting unknown marker types."""

    if (isinstance(value, bool) or not isinstance(value, int)
            or value < 0):
        return None
    return value


def _payload_version(payload: Mapping[str, Any]) -> int | None:
    """Return a payload's version, treating an absent marker as legacy v0."""

    if "schema_version" in payload:
        return _version(payload["schema_version"])
    if "version" in payload:
        return _version(payload["version"])
    return 0


def _is_supported_schema(payload: Mapping[str, Any]) -> bool:
    version = _payload_version(payload)
    return version is not None and version <= HISTORY_SCHEMA_VERSION


def _is_future_schema(payload: Mapping[str, Any]) -> bool:
    version = _payload_version(payload)
    return version is not None and version > HISTORY_SCHEMA_VERSION


def _has_canonical_v1_fields(payload: Mapping[str, Any]) -> bool:
    """Require the serialized v1 record contract before recovery promotion."""

    if not all(field in payload for field in _HISTORY_RECORD_FIELDS):
        return False
    for field in ("id", "workflow", "provider", "model", "status"):
        value = payload[field]
        if not isinstance(value, str) or not value.strip():
            return False
    if not isinstance(payload["timestamp"], str):
        return False
    for field in ("raw_text", "refined_text", "error"):
        value = payload[field]
        if value is not None and not isinstance(value, str):
            return False
    if payload["status"].strip().lower() not in HISTORY_STATUSES:
        return False
    return True


def _legacy_container_keys(payload: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        key for key in ("records", "entries", "history") if key in payload
    )


def _is_recoverable_snapshot(payload: Mapping[str, Any]) -> bool:
    """Return whether this executable can safely load a snapshot payload."""

    version = _payload_version(payload)
    if version is None or version > HISTORY_SCHEMA_VERSION:
        return False
    if version == HISTORY_SCHEMA_VERSION:
        raw_records = payload.get("records")
    else:
        legacy_keys = _legacy_container_keys(payload)
        if (not _legacy_containers_are_valid(payload)
                or len(legacy_keys) != 1
                or not _legacy_entries_are_safe(payload)):
            return False
        # Reject malformed legacy entries too; promoting a snapshot that the
        # normal load path would partially drop can erase the committed file.
        for key in ("records", "entries", "history"):
            source = payload.get(key)
            if isinstance(source, list) and any(
                    not isinstance(item, Mapping) for item in source):
                return False
        try:
            raw_records = _migrate_v0(payload).get("records")
        except HistoryStoreError:
            return False
    if not isinstance(raw_records, list):
        return False
    for item in raw_records:
        if not isinstance(item, Mapping):
            return False
        if (version == HISTORY_SCHEMA_VERSION
                and not _has_canonical_v1_fields(item)):
            return False
        try:
            HistoryRecord.from_mapping(item)
        except HistoryValidationError:
            return False
    return True


def _snapshot_container_is_valid(payload: Mapping[str, Any]) -> bool:
    version = _payload_version(payload)
    if version is None or version > HISTORY_SCHEMA_VERSION:
        return False
    if version == HISTORY_SCHEMA_VERSION:
        return isinstance(payload.get("records"), list)
    return (
        _legacy_containers_are_valid(payload)
        and len(_legacy_container_keys(payload)) == 1
    )


def _legacy_containers_are_valid(payload: Mapping[str, Any]) -> bool:
    return all(
        key not in payload or isinstance(payload[key], list)
        for key in ("records", "entries", "history")
    )


def _legacy_entries_are_safe(payload: Mapping[str, Any]) -> bool:
    """Reject legacy records whose explicit identifiers have invalid types."""

    for key in ("records", "entries", "history"):
        source = payload.get(key)
        if not isinstance(source, list):
            continue
        for item in source:
            if not isinstance(item, Mapping):
                return False
            for field in ("provider", "model"):
                if field in item and not isinstance(item[field], str):
                    return False
    return True


def _migrate_v0(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Migrate the pre-versioned prototype shape to schema version 1."""

    if not _legacy_containers_are_valid(payload):
        raise HistoryStoreError(
            "The legacy history file has an invalid records container")

    source = payload.get("records")
    if not isinstance(source, list):
        source = payload.get("entries")
    if not isinstance(source, list):
        source = payload.get("history")
    if not isinstance(source, list):
        source = []

    records: list[dict[str, Any]] = []
    for item in source:
        if not isinstance(item, Mapping):
            continue
        # Keep only names understood by HistoryRecord.from_mapping.  This is
        # also the migration boundary that drops prototype audio/API fields.
        records.append({
            key: item[key]
            for key in (
                "id", "record_id", "raw_text", "raw", "text", "refined_text",
                "refined", "workflow", "mode", "timestamp", "time", "provider",
                "model", "status", "error",
            )
            if key in item
        })
    return {"schema_version": HISTORY_SCHEMA_VERSION, "records": records}


HISTORY_MIGRATIONS: dict[int, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    0: _migrate_v0,
}


def migrate_history_payload(payload: Any) -> dict[str, Any]:
    """Apply ordered, idempotent history migrations without mutating input."""

    if not isinstance(payload, Mapping):
        return {"schema_version": HISTORY_SCHEMA_VERSION, "records": []}

    migrated: dict[str, Any] = dict(payload)
    version = _payload_version(migrated)
    if version is None or version > HISTORY_SCHEMA_VERSION:
        return migrated

    while version < HISTORY_SCHEMA_VERSION:
        migration = HISTORY_MIGRATIONS.get(version)
        if migration is None:
            break
        migrated = migration(migrated)
        next_version = _payload_version(migrated)
        if next_version is None or next_version <= version:
            break
        version = next_version

    records = migrated.get("records", [])
    if not isinstance(records, list):
        records = []
    migrated["schema_version"] = min(version, HISTORY_SCHEMA_VERSION)
    migrated["records"] = records
    return migrated


def _canonical_payload(records: list[HistoryRecord]) -> dict[str, Any]:
    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "records": [record.to_mapping() for record in records],
    }


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text through a same-directory temp file and atomic replacement."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        # Directory fsync is available on POSIX and is best effort on Windows.
        if os.name != "nt":
            try:
                directory_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _read_json_mapping(
    path: Path,
    *,
    strict: bool = False,
) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError) as error:
        if strict:
            raise HistoryStoreError(
                "The history file is unreadable or corrupt") from error
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


class HistoryStore:
    """An opt-in, versioned local history repository.

    The default ``enabled=False`` makes all reads and writes no-ops.  This is
    intentional: merely constructing the repository or receiving a transcript
    must not create a file or retain text.  ``delete_all`` remains available
    while disabled so a settings toggle can erase history immediately.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        enabled: bool = False,
        retention_days: int | None = DEFAULT_RETENTION_DAYS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(enabled, bool):
            raise HistoryValidationError("enabled must be boolean")
        if retention_days is not None and (
                isinstance(retention_days, bool)
                or not isinstance(retention_days, int)
                or retention_days < 0):
            raise HistoryValidationError(
                "retention_days must be a non-negative integer or None")
        self.path = Path(path)
        self.enabled = enabled
        self.retention_days = retention_days
        self._clock = clock or _utc_now
        self._lock = threading.RLock()

    def _now(self) -> datetime:
        return _coerce_timestamp(self._clock())

    def _temporary_paths(self) -> list[Path]:
        if not self.path.parent.exists():
            return []
        pattern = f".{self.path.name}.*.tmp"
        try:
            return list(self.path.parent.glob(pattern))
        except OSError as error:
            raise HistoryStoreError(
                "The history snapshots could not be enumerated") from error

    @staticmethod
    def _remove_best_effort(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    def _recover_interrupted_write_locked(self) -> dict[str, Any] | None:
        current = _read_json_mapping(self.path)
        temporary_payloads: list[tuple[float, str, Path, dict[str, Any]]] = []
        for candidate in self._temporary_paths():
            payload = _read_json_mapping(candidate)
            if payload is None:
                self._remove_best_effort(candidate)
                continue
            try:
                ordering = candidate.stat().st_mtime
            except OSError:
                ordering = 0.0
            temporary_payloads.append((ordering, candidate.name, candidate, payload))

        # A parseable snapshot in a schema this executable understands but
        # with invalid structure or records is not a recovery source. Keep
        # rejected snapshots until a valid primary or replacement is known;
        # when no such source exists they may be the only bytes left for
        # diagnosis/recovery. Future-schema snapshots remain untouched.
        supported_candidates = [
            item for item in temporary_payloads
            if _is_supported_schema(item[3])
        ]
        supported_temporary = [
            item for item in supported_candidates
            if _is_recoverable_snapshot(item[3])
        ]
        rejected_temporary = [
            item for item in supported_candidates
            if item not in supported_temporary
        ]

        if current is not None:
            current_version = _payload_version(current)
            if current_version is None:
                # An explicit but unknown marker is not a legacy v0 file.
                # Keep it in place so this executable cannot rewrite a
                # format whose version semantics it does not understand.
                return current
            if current_version > HISTORY_SCHEMA_VERSION:
                # Never let an older executable replace a future-schema
                # primary with an older temporary snapshot.  The newer file
                # remains intact for the executable that understands it.
                return current
            if not _snapshot_container_is_valid(current):
                # Keep every interrupted snapshot available while the
                # committed primary is still structurally invalid.  The load
                # path will fail closed without deleting the only recoverable
                # copy first. Record-level repairs remain eligible for a
                # newer validated snapshot below.
                return current
            # A complete temp written after the primary may represent a crash
            # between flushing the new snapshot and ``os.replace``. Recover it
            # when its mtime proves it is newer; an older leftover is from a
            # failed/retried write and the committed primary wins.
            if not supported_temporary:
                for _, _, candidate, _ in rejected_temporary:
                    self._remove_best_effort(candidate)
                return current
            try:
                current_mtime = self.path.stat().st_mtime
            except OSError:
                current_mtime = float("inf")
            newest = max(supported_temporary)
            selected: Path | None = None
            if newest[0] > current_mtime:
                _, _, selected, payload = newest
                try:
                    os.replace(selected, self.path)
                except OSError as error:
                    raise HistoryStoreError(
                        "The interrupted history write could not be recovered"
                    ) from error
                current = payload
                supported_temporary = [
                    item for item in supported_temporary if item[2] != selected
                ]
            for _, _, candidate, _ in [
                *rejected_temporary, *supported_temporary,
            ]:
                if candidate != selected:
                    self._remove_best_effort(candidate)
            return current

        if not temporary_payloads:
            try:
                primary_exists = self.path.exists()
            except OSError as error:
                raise HistoryStoreError(
                    "The history file could not be inspected") from error
            if primary_exists:
                # Re-read in strict mode to preserve a typed cause for both
                # malformed JSON and an unreadable file.
                _read_json_mapping(self.path, strict=True)
                raise HistoryStoreError(
                    "The history file is unreadable or corrupt")
            return None
        # Prefer a snapshot this executable understands. Unknown/future
        # snapshots remain untouched so a newer executable can recover them.
        if supported_temporary:
            candidates = supported_temporary
            selected_supported = True
        else:
            try:
                primary_exists = self.path.exists()
            except OSError as error:
                raise HistoryStoreError(
                    "The history file could not be inspected") from error
            if primary_exists:
                # A corrupt primary is recoverable only from a snapshot this
                # executable understands. Preserve its original bytes when
                # the only intact candidates use a future schema.
                _read_json_mapping(self.path, strict=True)
                raise HistoryStoreError(
                    "The history file is unreadable or corrupt")
            future_temporary = [
                item for item in temporary_payloads
                if _is_future_schema(item[3])
            ]
            if not future_temporary:
                # A supported-schema temp exists, but its structure is
                # damaged. Preserve it and fail closed rather than moving it
                # over the missing primary and later rewriting it as empty.
                raise HistoryStoreError(
                    "The interrupted history snapshot is structurally corrupt")
            candidates = future_temporary
            selected_supported = False
        _, _, selected, payload = max(candidates)
        try:
            os.replace(selected, self.path)
        except OSError as error:
            raise HistoryStoreError(
                "The interrupted history write could not be recovered") from error
        if selected_supported:
            for _, _, candidate, _ in [
                *rejected_temporary, *supported_temporary,
            ]:
                if candidate != selected:
                    self._remove_best_effort(candidate)
        return payload

    def _load_records_locked(self) -> list[HistoryRecord]:
        payload = self._recover_interrupted_write_locked()
        if payload is None:
            return []
        version = _payload_version(payload)
        if version is None:
            raise HistoryStoreError(
                "The history file has an invalid schema version")
        if version > HISTORY_SCHEMA_VERSION:
            raise UnsupportedHistorySchemaVersionError(
                f"History schema version {version} is newer than supported "
                f"version {HISTORY_SCHEMA_VERSION}")

        migrated = migrate_history_payload(payload)
        if version == HISTORY_SCHEMA_VERSION and not isinstance(
                payload.get("records"), list):
            # A current-schema document with a missing or non-list record
            # container is structural corruption, not an empty history.  Do
            # not canonicalize it to ``records: []``: doing so would erase
            # data that may still be recoverable from the original bytes.
            raise HistoryStoreError(
                "The history file has an invalid records container")
        if version < HISTORY_SCHEMA_VERSION and not _snapshot_container_is_valid(
                payload):
            raise HistoryStoreError(
                "The legacy history file has an ambiguous records container")
        raw_records = migrated.get("records", [])
        records: list[HistoryRecord] = []
        if isinstance(raw_records, list):
            for item in raw_records:
                if not isinstance(item, Mapping):
                    continue
                if (version == HISTORY_SCHEMA_VERSION
                        and not _has_canonical_v1_fields(item)):
                    continue
                try:
                    records.append(HistoryRecord.from_mapping(item))
                except HistoryValidationError:
                    # One malformed/partial entry must not make the rest of a
                    # user's history unavailable.  The next atomic rewrite
                    # removes only the malformed entry.
                    continue

        now = self._now()
        records = self._retained(records, now)
        canonical = _canonical_payload(records)
        if canonical != payload:
            self._write_payload_locked(canonical)
        return records

    def _write_payload_locked(self, payload: Mapping[str, Any]) -> None:
        try:
            serialized = json.dumps(
                payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
            _atomic_write_text(self.path, serialized)
        except (OSError, TypeError, ValueError) as error:
            raise HistoryStoreError("The history file could not be written") from error

    def _retained(self, records: list[HistoryRecord], now: datetime) -> list[HistoryRecord]:
        if self.retention_days is None:
            return records
        cutoff = now - timedelta(days=self.retention_days)
        return [record for record in records if record.timestamp >= cutoff]

    def list_records(self) -> list[HistoryRecord]:
        """Return retained records in persisted order, newest or oldest alike."""

        if not self.enabled:
            return []
        with self._lock:
            return list(self._load_records_locked())

    # A short alias makes the repository boundary convenient for UI adapters.
    load = list_records

    def append(self, record: HistoryRecord) -> HistoryRecord | None:
        """Append one typed record, or no-op when history is disabled."""

        if not isinstance(record, HistoryRecord):
            raise HistoryValidationError("append expects a HistoryRecord")
        if not self.enabled:
            return None
        with self._lock:
            records = self._load_records_locked()
            if any(existing.record_id == record.record_id for existing in records):
                raise HistoryValidationError(
                    f"history record id already exists: {record.record_id}")
            records = self._retained(records + [record], self._now())
            self._write_payload_locked(_canonical_payload(records))
        return record

    def add(
        self,
        *,
        raw_text: str | None = None,
        refined_text: str | None = None,
        workflow: str = "transcription",
        timestamp: datetime | str | None = None,
        provider: str = "unknown",
        model: str = "unknown",
        status: HistoryStatus = "success",
        error: str | None = None,
        record_id: str | None = None,
    ) -> HistoryRecord | None:
        """Build and append a record for callers that do not need a dataclass."""

        record = HistoryRecord(
            raw_text=raw_text,
            refined_text=refined_text,
            workflow=workflow,
            timestamp=self._now() if timestamp is None else timestamp,
            provider=provider,
            model=model,
            status=status,
            error=error,
            record_id=_new_record_id() if record_id is None else record_id,
        )
        return self.append(record)

    def delete_all(self) -> None:
        """Delete the history snapshot and any interrupted-write leftovers."""

        with self._lock:
            # Remove recovery snapshots first. If one cannot be unlinked, the
            # committed primary must remain so a later startup cannot
            # resurrect supposedly deleted transcripts from that snapshot.
            targets = [*self._temporary_paths(), self.path]
            for target in targets:
                try:
                    target.unlink()
                except FileNotFoundError:
                    continue
                except OSError as error:
                    raise HistoryStoreError(
                        "The history file could not be deleted") from error

    def _records_for_export(self) -> list[HistoryRecord]:
        if not self.enabled:
            raise HistoryDisabledError("History is disabled")
        return self.list_records()

    @staticmethod
    def _markdown_fence(value: str) -> str:
        length = 3
        while "`" * length in value:
            length += 1
        return "`" * length

    @classmethod
    def _as_text(cls, records: list[HistoryRecord]) -> str:
        chunks: list[str] = []
        for record in records:
            chunks.extend([
                f"Record: {record.record_id}",
                f"Timestamp: {_format_timestamp(record.timestamp)}",
                f"Workflow: {record.workflow}",
                f"Provider: {record.provider}",
                f"Model: {record.model}",
                f"Status: {record.status}",
            ])
            chunks.append("Raw transcript:")
            chunks.append(record.raw_text if record.raw_text is not None else "<not available>")
            chunks.append("Refined output:")
            chunks.append(
                record.refined_text
                if record.refined_text is not None else "<not available>")
            if record.error is not None:
                chunks.extend(["Error:", record.error])
            chunks.append("")
            chunks.append("=" * 72)
            chunks.append("")
        return "\n".join(chunks) if chunks else "ClarifyVoice transcription history\n"

    @classmethod
    def _as_markdown(cls, records: list[HistoryRecord]) -> str:
        chunks = ["# ClarifyVoice transcription history", ""]
        for index, record in enumerate(records, 1):
            chunks.extend([
                f"## {index}. {_format_timestamp(record.timestamp)} — {record.status}",
                "",
                f"- **Workflow:** `{record.workflow}`",
                f"- **Provider:** `{record.provider}`",
                f"- **Model:** `{record.model}`",
                f"- **Record ID:** `{record.record_id}`",
                "",
            ])
            for label, value in (
                ("Raw transcript", record.raw_text),
                ("Refined output", record.refined_text),
            ):
                chunks.extend([f"### {label}", ""])
                if value is None:
                    chunks.extend(["*Not available.*", ""])
                else:
                    fence = cls._markdown_fence(value)
                    chunks.extend([fence, value, fence, ""])
            if record.error is not None:
                chunks.extend(["### Error", "", record.error, ""])
        return "\n".join(chunks)

    @staticmethod
    def _format_name(destination: Path, requested: str | None) -> str:
        value = (requested or "").strip().lower().lstrip(".")
        if not value:
            suffix = destination.suffix.lower()
            value = {
                ".json": "json",
                ".md": "markdown",
                ".markdown": "markdown",
            }.get(suffix, "txt")
        aliases = {"text": "txt", "md": "markdown"}
        value = aliases.get(value, value)
        if value not in {"txt", "markdown", "json"}:
            raise HistoryValidationError(
                "format must be txt, markdown, or json")
        return value

    def export(
        self,
        destination: str | os.PathLike[str],
        *,
        format: str | None = None,
    ) -> Path:
        """Atomically export retained history as TXT, Markdown, or JSON."""

        records = self._records_for_export()
        target = Path(destination)
        try:
            if target.resolve() == self.path.resolve():
                raise HistoryValidationError(
                    "history export destination must differ from the history file")
        except OSError:
            # A not-yet-created destination still has a deterministic lexical
            # path; the atomic writer below performs the final filesystem check.
            pass
        selected = self._format_name(target, format)
        if selected == "json":
            text = json.dumps(
                _canonical_payload(records), indent=2, ensure_ascii=False) + "\n"
        elif selected == "markdown":
            text = self._as_markdown(records)
        else:
            text = self._as_text(records)
        try:
            _atomic_write_text(target, text)
        except (OSError, TypeError, ValueError) as error:
            raise HistoryStoreError("The history export could not be written") from error
        return target
