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
        source_key: str,
        sub_source: str,
        remote_unit_id: str,
        content_hashes: Sequence[tuple[str, str]],
    ) -> bool:
        values = VariantRef(
            source_key, sub_source, remote_unit_id,
            tuple(ContentHash(by, value) for by, value in content_hashes),
        )  # fmt: skip
        return self._service.check(values, root_id=root_id).blocked

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
        self, root_id: str | None, source_key: str, sub_source: str, series_ref: str
    ) -> bool:
        return self._service.check(
            SeriesBindingRef(source_key, sub_source, series_ref), root_id=root_id
        ).blocked
