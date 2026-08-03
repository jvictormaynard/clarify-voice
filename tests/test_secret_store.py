import json
import os
import tempfile
import traceback
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from secret_store import (
    DpapiSecretStore,
    PlaintextFileSecretStore,
    SecretStoreCorruptedError,
    SecretStoreUnavailableError,
    create_secret_store,
)


class SecretStoreContractTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "requires the real Windows DPAPI")
    def test_real_windows_dpapi_round_trip_all_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.dpapi.json"
            first = DpapiSecretStore(path)
            expected = {
                provider: f"non-production-{provider}-credential"
                for provider in ("gemini", "openai", "groq")
            }
            for provider, value in expected.items():
                first.set(provider, value)

            second = DpapiSecretStore(path)
            self.assertEqual(
                {provider: second.get(provider) for provider in expected},
                expected,
            )
            for provider in expected:
                second.delete(provider)

            self.assertFalse(path.exists())

    def test_plaintext_fallback_save_load_and_delete(self):
        temporary_root = "/tmp" if os.name != "nt" and Path("/tmp").is_dir() else None
        with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
            path = Path(directory) / "secrets.json"
            store = PlaintextFileSecretStore(path)

            store.set("openai", "test-openai-credential")
            self.assertEqual(store.get("openai"), "test-openai-credential")
            self.assertNotIn("openai_api_key", path.read_text(encoding="utf-8"))

            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            store.delete("openai")
            self.assertIsNone(store.get("openai"))
            self.assertFalse(path.exists())

    def test_dpapi_container_never_contains_plaintext(self):
        def protect(value):
            return bytes(byte ^ 0xA5 for byte in value)

        store_unprotect = protect
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.dpapi.json"
            store = DpapiSecretStore(path, protect, store_unprotect)
            value = "test-gemini-credential"

            store.set("gemini", value)

            self.assertEqual(store.get("gemini"), value)
            self.assertNotIn(value, path.read_text(encoding="utf-8"))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["backend"], "windows-dpapi")

    def test_corrupted_entry_raises_only_a_sanitized_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.dpapi.json"
            path.write_text(json.dumps({
                "version": 1,
                "backend": "windows-dpapi",
                "entries": {"groq": "not valid base64!"},
            }), encoding="utf-8")
            store = DpapiSecretStore(path, lambda value: value, lambda value: value)

            with self.assertRaises(SecretStoreCorruptedError) as raised:
                store.get("groq")

            self.assertNotIn("not valid base64", str(raised.exception))

    def test_empty_decrypted_entry_is_corrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.dpapi.json"
            path.write_text(json.dumps({
                "version": 1,
                "backend": "windows-dpapi",
                "entries": {"openai": "AA=="},
            }), encoding="utf-8")
            store = DpapiSecretStore(path, lambda value: value, lambda _value: b"")

            with self.assertRaises(SecretStoreCorruptedError):
                store.get("openai")

    def test_unreadable_container_is_unavailable_not_corrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.dpapi.json"
            path.mkdir()
            store = PlaintextFileSecretStore(path)

            with self.assertRaises(SecretStoreUnavailableError):
                store.get("openai")

    def test_unavailable_dpapi_error_does_not_echo_the_secret(self):
        value = "test-secret-must-not-appear"

        def unavailable(_value):
            raise RuntimeError(value)

        with tempfile.TemporaryDirectory() as directory:
            store = DpapiSecretStore(
                Path(directory) / "secrets.dpapi.json",
                unavailable,
                lambda protected: protected,
            )
            with self.assertRaises(SecretStoreUnavailableError) as raised:
                store.set("openai", value)

            self.assertNotIn(value, str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn(
                value,
                "".join(traceback.format_exception(raised.exception)),
            )

    def test_factory_uses_documented_non_windows_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            store = create_secret_store(directory, system="Linux")

            self.assertIsInstance(store, PlaintextFileSecretStore)
            self.assertEqual(store.path, Path(directory) / "secrets.json")

    def test_factory_accepts_case_insensitive_windows_name(self):
        with tempfile.TemporaryDirectory() as directory:
            store = create_secret_store(directory, system="windows")

            self.assertIsInstance(store, DpapiSecretStore)
            self.assertEqual(store.path, Path(directory) / "secrets.dpapi.json")

    def test_packaged_cli_self_test_reports_only_safe_metadata(self):
        import app

        output = StringIO()
        with redirect_stdout(output):
            result = app._run_cli(["secret-store-self-test"])

        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(
            payload["providers"], ["gemini", "openai", "groq"])
        self.assertNotIn("clarifyvoice-self-test", output.getvalue())

    def test_windows_factory_is_lazy_when_dpapi_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = create_secret_store(directory, system="Windows")

            self.assertIsInstance(store, DpapiSecretStore)
            self.assertIsNone(store.get("openai"))
            if os.name == "nt":
                store.set("openai", "test-openai-credential")
                self.assertEqual(
                    store.get("openai"), "test-openai-credential")
            else:
                with self.assertRaises(SecretStoreUnavailableError):
                    store.set("openai", "test-openai-credential")


if __name__ == "__main__":
    unittest.main()
