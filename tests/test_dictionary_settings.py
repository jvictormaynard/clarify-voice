from pathlib import Path
import tempfile
import unittest

from dictionary_settings import DictionarySettingsController
from dictionary_snippets import (
    DictionaryEntry,
    DictionarySnippetService,
    DictionarySnippets,
    DictionarySnippetsError,
    LocalDictionarySnippetsRepository,
    Snippet,
)


class DictionarySettingsControllerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "dictionary.json"
        service = DictionarySnippetService(
            LocalDictionarySnippetsRepository(self.path))
        self.controller = DictionarySettingsController(service)

    def tearDown(self):
        self.directory.cleanup()

    def test_crud_search_preview_and_reset_persist_through_service(self):
        self.controller.add_dictionary(
            "OpenWhispr", pronunciation="open whisper", aliases=("OW",))
        self.controller.add_snippet(";meet", "Agenda:\n1. Confirm scope")

        self.assertEqual(
            [(item.kind, item.index, item.label)
             for item in self.controller.search("WHIS")],
            [("dictionary", 0, "OpenWhispr")])
        self.assertEqual(
            [(item.kind, item.index, item.label)
             for item in self.controller.search("agenda")],
            [("snippet", 0, ";meet")])
        self.assertEqual(
            self.controller.preview("Use ;meet today"),
            "Use Agenda:\n1. Confirm scope today")

        self.controller.update_dictionary(
            0, "OpenWhisper", pronunciation="open whisper",
            aliases=("OW", "Open Whisper"), enabled=False)
        self.controller.update_snippet(
            0, ";meeting", "Meeting notes", case_sensitive=True)
        state = self.controller.state
        self.assertEqual(state.dictionary[0], DictionaryEntry(
            "OpenWhisper", "open whisper", ("OW", "Open Whisper"), False))
        self.assertEqual(state.snippets[0], Snippet(
            ";meeting", "Meeting notes", True, True))

        self.controller.delete_dictionary(0)
        self.controller.delete_snippet(0)
        self.assertEqual(self.controller.state, DictionarySnippets.empty())

        self.controller.add_snippet(";sig", "Regards")
        self.controller.reset()
        self.assertEqual(self.controller.state, DictionarySnippets.empty())
        self.assertEqual(
            LocalDictionarySnippetsRepository(self.path).load(),
            DictionarySnippets.empty())

    def test_failed_update_does_not_mutate_existing_profile(self):
        self.controller.add_dictionary("ClarifyVoice")
        before = self.controller.state

        with self.assertRaises(DictionarySnippetsError):
            self.controller.update_dictionary(0, "ClarifyVoice", aliases=("x", "X"))

        self.assertEqual(self.controller.state, before)
        self.assertEqual(
            LocalDictionarySnippetsRepository(self.path).load(), before)

    def test_invalid_indices_are_rejected_without_writing(self):
        before = self.controller.state
        for operation in (
                lambda: self.controller.delete_dictionary(0),
                lambda: self.controller.update_dictionary(0, "term"),
                lambda: self.controller.delete_snippet(0),
                lambda: self.controller.update_snippet(0, "x", "y"),
        ):
            with self.assertRaises(IndexError):
                operation()
        self.assertEqual(self.controller.state, before)

    def test_export_import_are_exposed_without_bypassing_validation(self):
        self.controller.add_snippet(";brb", "be right back")
        exported = self.controller.export_json()
        self.assertIn('"schema_version": 1', exported)

        self.controller.reset()
        imported = self.controller.import_json(exported)
        self.assertEqual(imported, self.controller.state)
        self.assertEqual(self.controller.preview(";brb"), "be right back")

        with self.assertRaises(DictionarySnippetsError):
            self.controller.import_json("{broken")
        self.assertEqual(self.controller.state, imported)


if __name__ == "__main__":
    unittest.main()
