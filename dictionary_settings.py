"""Settings-facing operations for the local dictionary and snippets.

The application UI is intentionally kept thin.  This controller owns the
validated CRUD/search operations used by Settings, while the persistence and
matching rules remain in :mod:`dictionary_snippets`.  Keeping the boundary
free of Tk widgets also makes the privacy and ordering guarantees easy to
exercise in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping
from typing import Iterable

from dictionary_snippets import (
    DictionaryEntry,
    DictionarySnippetService,
    DictionarySnippets,
    Snippet,
)


@dataclass(frozen=True)
class DictionarySettingsItem:
    """A stable list projection used by the Settings page."""

    kind: str
    index: int
    label: str
    detail: str
    enabled: bool
    # Dictionary metadata stays structured so the UI can localize its labels.
    # ``detail`` remains the plain replacement preview used by snippets.
    pronunciation: str = ""
    aliases: tuple[str, ...] = ()


class DictionarySettingsController:
    """Expose local dictionary/snippet profile operations to the UI.

    Every mutation creates a new validated :class:`DictionarySnippets` value
    and delegates persistence to ``DictionarySnippetService.replace``.  A
    failed validation therefore leaves both the in-memory profile and the
    on-disk document untouched.
    """

    def __init__(self, service: DictionarySnippetService) -> None:
        self.service = service

    @property
    def state(self) -> DictionarySnippets:
        return self.service.state

    def search(self, query: str = "") -> tuple[DictionarySettingsItem, ...]:
        """Return dictionary and snippet rows matching ``query``.

        Dictionary entries are listed first, followed by snippets, preserving
        the order in the profile.  Matching is case-insensitive and searches
        all editable fields so aliases and replacement text are discoverable.
        """

        needle = str(query or "").strip().casefold()
        state = self.service.state
        items: list[DictionarySettingsItem] = []
        for index, entry in enumerate(state.dictionary):
            haystack = " ".join((entry.term, entry.pronunciation, *entry.aliases))
            if not needle or needle in haystack.casefold():
                items.append(DictionarySettingsItem(
                    "dictionary", index, entry.term, "", entry.enabled,
                    pronunciation=entry.pronunciation, aliases=entry.aliases))
        for index, snippet in enumerate(state.snippets):
            detail = snippet.replacement.replace("\n", " ")
            haystack = f"{snippet.trigger} {snippet.replacement}"
            if not needle or needle in haystack.casefold():
                items.append(DictionarySettingsItem(
                    "snippet", index, snippet.trigger, detail,
                    snippet.enabled))
        return tuple(items)

    def add_dictionary(
        self,
        term: str,
        pronunciation: str = "",
        aliases: Iterable[str] = (),
        enabled: bool = True,
    ) -> DictionarySnippets:
        state = self.service.state
        entry = DictionaryEntry(
            term=term,
            pronunciation=pronunciation,
            aliases=tuple(aliases),
            enabled=enabled,
        )
        return self.service.replace(DictionarySnippets(
            dictionary=(*state.dictionary, entry),
            snippets=state.snippets,
        ))

    def update_dictionary(
        self,
        index: int,
        term: str,
        pronunciation: str = "",
        aliases: Iterable[str] = (),
        enabled: bool = True,
    ) -> DictionarySnippets:
        state = self.service.state
        entries = list(state.dictionary)
        self._check_index(index, entries, "dictionary")
        entries[index] = DictionaryEntry(
            term=term,
            pronunciation=pronunciation,
            aliases=tuple(aliases),
            enabled=enabled,
        )
        return self.service.replace(DictionarySnippets(
            dictionary=tuple(entries), snippets=state.snippets))

    def delete_dictionary(self, index: int) -> DictionarySnippets:
        state = self.service.state
        entries = list(state.dictionary)
        self._check_index(index, entries, "dictionary")
        del entries[index]
        return self.service.replace(DictionarySnippets(
            dictionary=tuple(entries), snippets=state.snippets))

    def add_snippet(
        self,
        trigger: str,
        replacement: str,
        enabled: bool = True,
        case_sensitive: bool = False,
    ) -> DictionarySnippets:
        state = self.service.state
        snippet = Snippet(
            trigger=trigger,
            replacement=replacement,
            enabled=enabled,
            case_sensitive=case_sensitive,
        )
        return self.service.replace(DictionarySnippets(
            dictionary=state.dictionary,
            snippets=(*state.snippets, snippet),
        ))

    def update_snippet(
        self,
        index: int,
        trigger: str,
        replacement: str,
        enabled: bool = True,
        case_sensitive: bool = False,
    ) -> DictionarySnippets:
        state = self.service.state
        snippets = list(state.snippets)
        self._check_index(index, snippets, "snippets")
        snippets[index] = Snippet(
            trigger=trigger,
            replacement=replacement,
            enabled=enabled,
            case_sensitive=case_sensitive,
        )
        return self.service.replace(DictionarySnippets(
            dictionary=state.dictionary, snippets=tuple(snippets)))

    def delete_snippet(self, index: int) -> DictionarySnippets:
        state = self.service.state
        snippets = list(state.snippets)
        self._check_index(index, snippets, "snippets")
        del snippets[index]
        return self.service.replace(DictionarySnippets(
            dictionary=state.dictionary, snippets=tuple(snippets)))

    def reset(self) -> DictionarySnippets:
        """Clear both collections through the same atomic save boundary."""

        return self.service.replace(DictionarySnippets.empty())

    def preview(self, text: str) -> str:
        """Expand a sample using the active local snippets."""

        return self.service.expand(text)

    def export_json(self) -> str:
        return self.service.export_json()

    def import_json(
        self, document: str | bytes | Mapping[str, object]
    ) -> DictionarySnippets:
        return self.service.import_json(document)

    def export_to(self, destination: str | Path) -> Path:
        return self.service.repository.export_to(destination)

    @staticmethod
    def _check_index(index: int, values: list[object], collection: str) -> None:
        if not isinstance(index, int) or isinstance(index, bool):
            raise IndexError(f"invalid {collection} index")
        if not 0 <= index < len(values):
            raise IndexError(f"{collection} index is out of range")
