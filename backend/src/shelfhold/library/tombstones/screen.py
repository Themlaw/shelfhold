"""The tombstone journal as the restore asks about it (EF-INT-026, I36, EF-BIB-053).

`TombstoneScreenAdapter` is what `reconcile --adopt-after-restore` is given: it replays the
journals (before anything is re-adopted), says which roots cannot be reconciled yet, and answers
"may this come back?" from plain values, so the export context never imports a tombstone class.
Nothing here deletes anything.
"""

from collections.abc import Mapping, Sequence

from shelfhold.library.tombstones.service import TombstoneRoot, TombstoneService
from shelfhold.library.tombstones.values import ContentHash, PlaceRef, SeriesBindingRef, VariantRef

__all__ = ["TombstoneScreenAdapter"]


class TombstoneScreenAdapter:
    def __init__(self, service: TombstoneService) -> None:
        self._service = service

    def replay(self, roots: Sequence[tuple[str, str, bool, tuple[str, ...]]]) -> None:
        """Every journal, and the table, before any re-adoption: `root_id, path, mounted,
        predecessor root ids` (a journal sealed under a root's former identity still opens)."""
        self._service.replay(
            [TombstoneRoot(root_id, path, mounted, predecessors)
             for root_id, path, mounted, predecessors in roots]
        )  # fmt: skip

    def root_blocked(self, root_id: str) -> str | None:
        report = self._service.report
        if root_id in report.unreadable_roots:
            return "journal_unreadable"
        if not self._service.reconcile_allowed(root_id):
            return "key_unknown"
        return None

    def unverified(self) -> int:
        return len(self._service.report.inert)

    def variant_blocked(
        self,
        root_id: str | None,
        plugin_id: str,
        source_key: str,
        remote_unit_id: str,
        content_hashes: Sequence[tuple[str, str]],
    ) -> bool:
        """The layout the purge records (EF-BIB-053): `source_key` holds the plugin id and
        `sub_source` the source key of the received item."""
        values = VariantRef(
            plugin_id, source_key, remote_unit_id,
            tuple(ContentHash(by, value) for by, value in content_hashes),
        )  # fmt: skip
        return self._service.check(values, root_id=root_id).blocked

    def content_blocked(
        self, root_id: str | None, content_hashes: Sequence[tuple[str, str]]
    ) -> bool:
        if not content_hashes:
            return False
        if root_id is not None and not self._service.reconcile_allowed(root_id):
            return True
        return self._service.check_content(ContentHash(by, v) for by, v in content_hashes)

    def place_blocked(
        self,
        root_id: str | None,
        edition_key: Mapping[str, str],
        track: str,
        number_segment: str,
        number_raw: str,
        part: str,
    ) -> bool:
        place = PlaceRef.of(
            edition_key, track=track, number_segment=number_segment, number_raw=number_raw,
            part=part,
        )  # fmt: skip
        return self._service.check(place, root_id=root_id).blocked

    def series_blocked(
        self, root_id: str | None, plugin_id: str, source_key: str, series_ref: str
    ) -> bool:
        return self._service.check(
            SeriesBindingRef(plugin_id, source_key, series_ref), root_id=root_id
        ).blocked
