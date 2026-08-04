import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import history_store
from history_store import (
    HISTORY_SCHEMA_VERSION,
    HistoryDisabledError,
    HistoryRecord,
    HistoryStore,
    HistoryStoreError,
    UnsupportedHistorySchemaVersionError,
    migrate_history_payload,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def fixed_clock():
    return NOW


class HistoryStoreTests(unittest.TestCase):
    def test_history_is_disabled_by_default_and_creates_no_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            store = HistoryStore(path)

            self.assertIsNone(store.add(raw_text="must not be retained"))
            self.assertEqual(store.list_records(), [])
            self.assertFalse(path.exists())
            with self.assertRaises(HistoryDisabledError):
                store.export(Path(directory) / "history.txt")

    def test_unicode_and_multiline_success_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            store = HistoryStore(path, enabled=True, retention_days=None,
                                 clock=fixed_clock)
            stored = store.add(
                raw_text="Olá, mundo!\n第二行",
                refined_text="Olá — segunda linha\n✅",
                workflow="rewrite",
                provider="openai",
                model="gpt-test",
            )

            self.assertIsInstance(stored, HistoryRecord)
            reloaded = HistoryStore(path, enabled=True, retention_days=None,
                                    clock=fixed_clock).list_records()
            self.assertEqual(len(reloaded), 1)
            self.assertEqual(reloaded[0].raw_text, "Olá, mundo!\n第二行")
            self.assertEqual(reloaded[0].refined_text, "Olá — segunda linha\n✅")
            self.assertEqual(reloaded[0].timestamp, NOW)
            self.assertEqual(reloaded[0].workflow, "rewrite")

    def test_partial_and_error_records_keep_missing_text_and_safe_error(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(
                Path(directory) / "history.json",
                enabled=True,
                retention_days=None,
                clock=fixed_clock,
            )
            store.add(status="partial", raw_text="partial", refined_text=None)
            store.add(
                status="error",
                raw_text=None,
                refined_text=None,
                error="HTTP timeout; api_key=live-secret",
            )

            records = store.list_records()
            self.assertEqual([record.status for record in records], [
                "partial", "error",
            ])
            self.assertIsNone(records[1].raw_text)
            self.assertEqual(records[1].error, "HTTP timeout; api_key=<redacted>")
            self.assertNotIn("live-secret", Path(store.path).read_text(encoding="utf-8"))

    def test_authorization_and_bearer_values_are_redacted_in_error_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(
                Path(directory) / "history.json",
                enabled=True,
                retention_days=None,
                clock=fixed_clock,
            )
            store.add(
                status="error",
                error=(
                    "Authorization: Bearer sk-secret; "
                    "Authorization: Basic basic-secret; Bearer another-secret"
                ),
            )

            error = store.list_records()[0].error
            self.assertEqual(
                error,
                "Authorization: Bearer <redacted>; "
                "Authorization: Basic <redacted>; Bearer <redacted>",
            )
            persisted = Path(store.path).read_text(encoding="utf-8")
            self.assertNotIn("sk-secret", persisted)
            self.assertNotIn("basic-secret", persisted)
            self.assertNotIn("another-secret", persisted)

    def test_generic_token_and_credential_fields_are_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(
                Path(directory) / "history.json",
                enabled=True,
                retention_days=None,
                clock=fixed_clock,
            )
            store.add(
                status="error",
                error=(
                    '{"token": "token-secret"} '
                    "credential=credential-secret"
                ),
            )

            error = store.list_records()[0].error
            self.assertEqual(
                error,
                '{"token": "<redacted>"} credential=<redacted>',
            )
            persisted = Path(store.path).read_text(encoding="utf-8")
            self.assertNotIn("token-secret", persisted)
            self.assertNotIn("credential-secret", persisted)

    def test_quoted_multiword_credentials_are_redacted_as_one_value(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(
                Path(directory) / "history.json",
                enabled=True,
                retention_days=None,
                clock=fixed_clock,
            )
            store.add(
                status="error",
                error="password='correct horse battery staple'",
            )

            error = store.list_records()[0].error
            self.assertEqual(error, "password='<redacted>'")
            persisted = Path(store.path).read_text(encoding="utf-8")
            for value in ("correct", "horse", "battery", "staple"):
                self.assertNotIn(value, persisted)

    def test_escaped_quote_in_credential_is_redacted_as_one_value(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(
                Path(directory) / "history.json",
                enabled=True,
                retention_days=None,
                clock=fixed_clock,
            )
            store.add(
                status="error",
                error='{\"password\": \"abc\\\"def\"}',
            )

            error = store.list_records()[0].error
            self.assertEqual(error, '{\"password\": \"<redacted>\"}')
            persisted = Path(store.path).read_text(encoding="utf-8")
            self.assertNotIn("abc", persisted)
            self.assertNotIn("def", persisted)

    def test_v0_migration_is_idempotent_and_drops_unsupported_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            path.write_text(json.dumps({
                "version": 0,
                "history": [{
                    "id": "legacy-1",
                    "text": "linha antiga",
                    "time": NOW.isoformat(),
                    "mode": "transcription",
                    "provider": "gemini",
                    "model": "gemini-test",
                    "audio_path": "C:/private/audio.wav",
                    "api_key": "must-not-survive",
                }],
            }), encoding="utf-8")

            store = HistoryStore(path, enabled=True, retention_days=None,
                                 clock=fixed_clock)
            records = store.list_records()
            self.assertEqual(records[0].raw_text, "linha antiga")
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], HISTORY_SCHEMA_VERSION)
            self.assertEqual(set(payload["records"][0]), {
                "id", "raw_text", "refined_text", "workflow", "timestamp",
                "provider", "model", "status", "error",
            })
            self.assertNotIn("must-not-survive", path.read_text(encoding="utf-8"))
            self.assertEqual(
                migrate_history_payload(payload),
                migrate_history_payload(migrate_history_payload(payload)),
            )

    def test_retention_is_applied_on_startup_and_persisted(self):
        old = NOW.replace(day=1)
        recent = NOW.replace(day=3)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            seed = HistoryStore(path, enabled=True, retention_days=None,
                                 clock=fixed_clock)
            seed.add(raw_text="old", timestamp=old)
            seed.add(raw_text="recent", timestamp=recent)

            retained = HistoryStore(path, enabled=True, retention_days=2,
                                    clock=fixed_clock).list_records()
            self.assertEqual([record.raw_text for record in retained], ["recent"])
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(persisted["records"]), 1)

    def test_delete_all_removes_primary_and_interrupted_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            store = HistoryStore(path, enabled=True, retention_days=None,
                                 clock=fixed_clock)
            store.add(raw_text="erase me")
            temporary = Path(directory) / ".history.json.leftover.tmp"
            temporary.write_text("{}", encoding="utf-8")

            store.delete_all()
            self.assertFalse(path.exists())
            self.assertFalse(temporary.exists())
            self.assertEqual(store.list_records(), [])

    def test_exports_preserve_unicode_multiline_partial_and_error_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = HistoryStore(root / "history.json", enabled=True,
                                 retention_days=None, clock=fixed_clock)
            store.add(raw_text="raw\nwith ``` fence\n世界", refined_text="refined ✅")
            store.add(status="error", error="provider unavailable")

            txt = store.export(root / "history.txt")
            markdown = store.export(root / "history.md")
            exported_json = store.export(root / "history.json.export", format="json")

            self.assertIn("raw\nwith ``` fence\n世界", txt.read_text(encoding="utf-8"))
            self.assertIn("provider unavailable", txt.read_text(encoding="utf-8"))
            self.assertIn("```raw", markdown.read_text(encoding="utf-8")
                          .replace("\n", ""))
            self.assertIn("世界", markdown.read_text(encoding="utf-8"))
            payload = json.loads(exported_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], HISTORY_SCHEMA_VERSION)
            self.assertEqual(len(payload["records"]), 2)
            self.assertNotIn("audio", exported_json.read_text(encoding="utf-8").lower())
            self.assertNotIn("api_key", exported_json.read_text(encoding="utf-8").lower())

    def test_failed_atomic_write_keeps_previous_snapshot_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            store = HistoryStore(path, enabled=True, retention_days=None,
                                 clock=fixed_clock)
            store.add(raw_text="committed")
            before = path.read_bytes()

            with patch.object(history_store.os, "replace",
                              side_effect=OSError("simulated interruption")):
                with self.assertRaises(HistoryStoreError):
                    store.add(raw_text="must not replace")

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(
                [record.raw_text for record in store.list_records()], ["committed"])
            self.assertEqual(list(root for root in Path(directory).glob(".*.tmp")), [])

    def test_valid_interrupted_snapshot_is_recovered_when_primary_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "history.json"
            candidate = root / ".history.json.crashed.tmp"
            record = HistoryRecord(
                raw_text="recovered", timestamp=NOW, provider="local_asr",
                model="ggml-small")
            candidate.write_text(json.dumps({
                "schema_version": HISTORY_SCHEMA_VERSION,
                "records": [record.to_mapping()],
            }), encoding="utf-8")

            records = HistoryStore(path, enabled=True, retention_days=None,
                                   clock=fixed_clock).list_records()
            self.assertEqual([item.raw_text for item in records], ["recovered"])
            self.assertTrue(path.exists())
            self.assertFalse(candidate.exists())

    def test_future_snapshot_is_preserved_when_supported_snapshot_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "history.json"
            supported_candidate = root / ".history.json.supported.tmp"
            future_candidate = root / ".history.json.future.tmp"
            supported = HistoryRecord(
                raw_text="supported", timestamp=NOW, provider="openai",
                model="gpt-test")
            supported_candidate.write_text(json.dumps({
                "schema_version": HISTORY_SCHEMA_VERSION,
                "records": [supported.to_mapping()],
            }), encoding="utf-8")
            future_candidate.write_text(json.dumps({
                "schema_version": HISTORY_SCHEMA_VERSION + 1,
                "records": [{"raw_text": "future"}],
            }), encoding="utf-8")

            records = HistoryStore(path, enabled=True, retention_days=None,
                                   clock=fixed_clock).list_records()
            self.assertEqual([item.raw_text for item in records], ["supported"])
            self.assertFalse(supported_candidate.exists())
            self.assertTrue(future_candidate.exists())
            self.assertEqual(
                json.loads(future_candidate.read_text(encoding="utf-8"))[
                    "schema_version"],
                HISTORY_SCHEMA_VERSION + 1,
            )

    def test_newer_interrupted_snapshot_wins_over_an_older_primary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "history.json"
            store = HistoryStore(path, enabled=True, retention_days=None,
                                 clock=fixed_clock)
            store.add(raw_text="older")
            candidate = root / ".history.json.crashed.tmp"
            newer = HistoryRecord(
                raw_text="newer", timestamp=NOW, provider="openai",
                model="gpt-test")
            candidate.write_text(json.dumps({
                "schema_version": HISTORY_SCHEMA_VERSION,
                "records": [newer.to_mapping()],
            }), encoding="utf-8")
            target_mtime = path.stat().st_mtime
            os.utime(candidate, (target_mtime + 1, target_mtime + 1))

            records = HistoryStore(path, enabled=True, retention_days=None,
                                   clock=fixed_clock).list_records()
            self.assertEqual([item.raw_text for item in records], ["newer"])
            self.assertFalse(candidate.exists())

    def test_future_schema_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            original = {"schema_version": HISTORY_SCHEMA_VERSION + 1,
                        "records": [{"private": "future"}]}
            path.write_text(json.dumps(original), encoding="utf-8")
            store = HistoryStore(path, enabled=True, retention_days=None,
                                 clock=fixed_clock)
            with self.assertRaises(UnsupportedHistorySchemaVersionError):
                store.list_records()
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_future_primary_is_not_replaced_by_supported_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "history.json"
            original = {
                "schema_version": HISTORY_SCHEMA_VERSION + 1,
                "records": [{"raw_text": "future-primary"}],
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            candidate = root / ".history.json.supported.tmp"
            candidate.write_text(json.dumps({
                "schema_version": HISTORY_SCHEMA_VERSION,
                "records": [HistoryRecord(
                    raw_text="older-snapshot", timestamp=NOW,
                    provider="openai", model="gpt-test").to_mapping()],
            }), encoding="utf-8")
            target_mtime = path.stat().st_mtime
            os.utime(candidate, (target_mtime + 1, target_mtime + 1))
            before = path.read_bytes()

            store = HistoryStore(path, enabled=True, retention_days=None,
                                 clock=fixed_clock)
            with self.assertRaises(UnsupportedHistorySchemaVersionError):
                store.list_records()
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(candidate.exists())

    def test_corrupt_primary_is_preserved_when_only_future_snapshot_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "history.json"
            path.write_text('{"schema_version": 1,', encoding="utf-8")
            before = path.read_bytes()
            candidate = root / ".history.json.future.tmp"
            candidate.write_text(json.dumps({
                "schema_version": HISTORY_SCHEMA_VERSION + 1,
                "records": [{"raw_text": "future"}],
            }), encoding="utf-8")

            store = HistoryStore(path, enabled=True, retention_days=None,
                                 clock=fixed_clock)
            with self.assertRaises(HistoryStoreError):
                store.list_records()
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(candidate.exists())

    def test_corrupt_primary_fails_closed_and_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            path.write_text('{"schema_version": 1,', encoding="utf-8")
            before = path.read_bytes()
            store = HistoryStore(path, enabled=True, retention_days=None,
                                 clock=fixed_clock)

            with self.assertRaises(HistoryStoreError):
                store.list_records()
            with self.assertRaises(HistoryStoreError):
                store.add(raw_text="must not replace corruption")
            self.assertEqual(path.read_bytes(), before)

    def test_v0_invalid_records_container_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            path.write_text(json.dumps({
                "version": 0,
                "records": {"text": "must-not-become-empty"},
            }), encoding="utf-8")
            before = path.read_bytes()
            store = HistoryStore(path, enabled=True, retention_days=None,
                                 clock=fixed_clock)

            with self.assertRaises(HistoryStoreError):
                store.list_records()
            with self.assertRaises(HistoryStoreError):
                store.add(raw_text="must not erase legacy corruption")
            self.assertEqual(path.read_bytes(), before)

    def test_current_schema_invalid_records_container_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            path.write_text(json.dumps({
                "schema_version": HISTORY_SCHEMA_VERSION,
                "records": {"raw_text": "must not be treated as empty"},
            }), encoding="utf-8")
            before = path.read_bytes()
            store = HistoryStore(path, enabled=True, retention_days=None,
                                 clock=fixed_clock)

            with self.assertRaises(HistoryStoreError):
                store.list_records()
            with self.assertRaises(HistoryStoreError):
                store.add(raw_text="must not erase the object")
            self.assertEqual(path.read_bytes(), before)

    def test_invalid_current_schema_snapshot_cannot_replace_valid_primary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "history.json"
            store = HistoryStore(path, enabled=True, retention_days=None,
                                 clock=fixed_clock)
            store.add(raw_text="committed")
            before = path.read_bytes()
            candidate = root / ".history.json.invalid.tmp"
            candidate.write_text(json.dumps({
                "schema_version": HISTORY_SCHEMA_VERSION,
                "records": {"raw_text": "must-not-replace"},
            }), encoding="utf-8")
            target_mtime = path.stat().st_mtime
            os.utime(candidate, (target_mtime + 1, target_mtime + 1))

            records = store.list_records()
            self.assertEqual([item.raw_text for item in records], ["committed"])
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(candidate.exists())

    def test_invalid_legacy_snapshot_cannot_replace_valid_primary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "history.json"
            store = HistoryStore(path, enabled=True, retention_days=None,
                                 clock=fixed_clock)
            store.add(raw_text="committed")
            before = path.read_bytes()
            candidate = root / ".history.json.invalid-legacy.tmp"
            candidate.write_text(json.dumps({
                "version": 0,
                "records": {"text": "must-not-replace"},
            }), encoding="utf-8")
            target_mtime = path.stat().st_mtime
            os.utime(candidate, (target_mtime + 1, target_mtime + 1))

            records = store.list_records()
            self.assertEqual([item.raw_text for item in records], ["committed"])
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(candidate.exists())

    def test_future_interrupted_snapshot_cannot_replace_supported_primary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "history.json"
            store = HistoryStore(path, enabled=True, retention_days=None,
                                 clock=fixed_clock)
            store.add(raw_text="supported")
            candidate = root / ".history.json.future.tmp"
            candidate.write_text(json.dumps({
                "schema_version": HISTORY_SCHEMA_VERSION + 1,
                "records": [{"raw_text": "future"}],
            }), encoding="utf-8")
            target_mtime = path.stat().st_mtime
            os.utime(candidate, (target_mtime + 1, target_mtime + 1))

            records = HistoryStore(path, enabled=True, retention_days=None,
                                   clock=fixed_clock).list_records()
            self.assertEqual([item.raw_text for item in records], ["supported"])
            self.assertTrue(candidate.exists())
            self.assertEqual(
                json.loads(candidate.read_text(encoding="utf-8"))["schema_version"],
                HISTORY_SCHEMA_VERSION + 1,
            )


if __name__ == "__main__":
    unittest.main()
