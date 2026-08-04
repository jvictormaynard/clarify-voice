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


_SENSITIVE_ERROR_PATTERNS = (
    (
        re.compile(
            r"(?i)(api[_ -]?key|access[_ -]?token|authorization|"
            r"client[_ -]?secret|password|secret)\s*[:=]\s*"
            r"(?:bearer\s+)?([^\s,;&]+)"),
        r"\1=<redacted>",
    ),
    (
        re.compile(r"(?i)(bearer)\s+([^\s,;&]+)"),
        r"\1 <redacted>",
    ),
    (
        re.compile(r"(?i)([?&](?:api[_-]?key|access[_-]?token|token)=)"
                   r"[^&#\s]+"),
        r"\1<redacted>",
    ),
)


def _safe_error(value: str | None) -> str | None:
    """Keep concise error metadata without persisting obvious credentials."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise HistoryValidationError("error must be text or None")
    if not value:
        return None
    sanitized = value
    for pattern, replacement in _SENSITIVE_ERROR_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
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


def _version(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _migrate_v0(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Migrate the pre-versioned prototype shape to schema version 1."""

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
    version = _version(migrated.get("schema_version", migrated.get("version")))
    if version > HISTORY_SCHEMA_VERSION:
        return migrated

    while version < HISTORY_SCHEMA_VERSION:
        migration = HISTORY_MIGRATIONS.get(version)
        if migration is None:
            break
        migrated = migration(migrated)
        next_version = _version(migrated.get("schema_version"))
        if next_version <= version:
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
        except OSError:
            return []

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

        if current is not None:
            # A complete temp written after the primary may represent a crash
            # between flushing the new snapshot and ``os.replace``. Recover it
            # when its mtime proves it is newer; an older leftover is from a
            # failed/retried write and the committed primary wins.
            supported_temporary = [
                item for item in temporary_payloads
                if _version(item[3].get(
                    "schema_version", item[3].get("version")))
                <= HISTORY_SCHEMA_VERSION
            ]
            for item in temporary_payloads:
                if item not in supported_temporary:
                    self._remove_best_effort(item[2])
            if not supported_temporary:
                return current
            try:
                current_mtime = self.path.stat().st_mtime
            except OSError:
                current_mtime = float("inf")
            newest = max(supported_temporary)
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
            for _, _, candidate, _ in supported_temporary:
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
        _, _, selected, payload = max(temporary_payloads)
        try:
            os.replace(selected, self.path)
        except OSError as error:
            raise HistoryStoreError(
                "The interrupted history write could not be recovered") from error
        for _, _, candidate, _ in temporary_payloads:
            if candidate != selected:
                self._remove_best_effort(candidate)
        return payload

    def _load_records_locked(self) -> list[HistoryRecord]:
        payload = self._recover_interrupted_write_locked()
        if payload is None:
            return []
        version = _version(payload.get("schema_version", payload.get("version")))
        if version > HISTORY_SCHEMA_VERSION:
            raise UnsupportedHistorySchemaVersionError(
                f"History schema version {version} is newer than supported "
                f"version {HISTORY_SCHEMA_VERSION}")

        migrated = migrate_history_payload(payload)
        raw_records = migrated.get("records", [])
        records: list[HistoryRecord] = []
        if isinstance(raw_records, list):
            for item in raw_records:
                if not isinstance(item, Mapping):
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
            targets = [self.path, *self._temporary_paths()]
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
