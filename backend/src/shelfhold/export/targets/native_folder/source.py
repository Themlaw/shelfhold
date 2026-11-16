"""The native folder destination for the host: what it is given and what it answers (EF-EXP-010).

`NativeFolderSource` is the `DestinationSource` of the fan-out for the targets of kind
`native_folder`: it reads the items the host planned for a binding (the Editions filed on its
shelf, admitted by the barrier, in the format the binding is served) and has the destination
describe the plan. The destination never reads the database and never touches the disk; this class
is the only one that reads, and what it gives the destination is plain values.

Also here, for the bytes: the series name of an item and its ComicInfo, so that the host serves each
chapter with a ComicInfo made for it (Translator = the team) and the cover of the series: the first
page of its first chapter, as it is (Mihon reads a file named `cover.*` by its content).
"""

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.export.application.plan_executor import PlanBinding
from shelfhold.export.application.rendering import item_series_name
from shelfhold.export.application.target_state import DestinationPausedError
from shelfhold.export.domain.bindings import BindingError
from shelfhold.export.domain.native_folder import COVER_STEM
from shelfhold.export.domain.path_map import clean_absolute
from shelfhold.export.targets.native_folder.destination import (
    ChapterSpec,
    NativeFolderDestination,
    SeriesSpec,
    cover_ref,
)
from shelfhold.export.targets.native_folder.names import series_folder
from shelfhold.infrastructure.schema import catalog, export, platform
from shelfhold.sdk.api_call import ApiCall
from shelfhold.sdk.export_target import TargetCapabilities
from shelfhold.sdk.fsops import FsOpPlan

__all__ = ["KIND", "NativeFolderSource", "native_series"]

KIND = "native_folder"
_KINDS = {"unit": False, "volume": True}  # the items that make a file; value: is it a volume


def native_series(connection: Connection, native_formats: Mapping[str, str]) -> list[SeriesSpec]:
    """The series of the items planned for a binding, in the order of their names. An item that
    is not a chapter or a volume, or has no native format, makes no file here."""
    if not native_formats:
        return []
    item, coverage, unit = export.export_item, export.unit_coverage, catalog.unit
    edition, work = catalog.edition, catalog.work
    rows = connection.execute(
        sa.select(
            item.c.id, item.c.kind, item.c.frozen_attrs, coverage.c.position,
            unit.c.number_norm, unit.c.track, unit.c.title, work.c.title.label("work_title"),
        )
        .select_from(
            item.join(
                coverage,
                sa.and_(
                    coverage.c.carrier_type == "export_item", coverage.c.carrier_id == item.c.id
                ),
            )
            .join(unit, unit.c.id == coverage.c.unit_id)
            .join(edition, edition.c.id == item.c.edition_id)
            .join(work, work.c.id == edition.c.work_id)
        )
        .where(item.c.id.in_(sorted(native_formats)))
        .order_by(item.c.id, coverage.c.position)
    )  # fmt: skip
    seen: set[str] = set()
    grouped: dict[str, list[ChapterSpec]] = {}
    for row in rows:
        if row.id in seen or row.kind not in _KINDS:
            continue
        seen.add(row.id)
        fmt = native_formats[row.id]
        if fmt not in ("cbz", "epub"):
            continue
        volume = _KINDS[row.kind]
        frozen = row.frozen_attrs or {}
        number = (
            (str(frozen.get("number_norm") or "") or row.number_norm) if volume else row.number_norm
        )
        main = volume or row.track == "main"
        name = item_series_name(frozen, row.work_title)
        grouped.setdefault(name, []).append(
            ChapterSpec(row.id, number if main else None, row.title or "", fmt, volume)  # type: ignore[arg-type]
        )
    return [
        SeriesSpec(name, tuple(chapters), _cover_item(chapters))
        for name, chapters in sorted(grouped.items())
    ]


def _cover_item(chapters: Sequence[ChapterSpec]) -> str | None:
    """The first chapter that is a CBZ: its first page is the cover (an EPUB gives none here)."""
    archives = [c for c in chapters if c.format == "cbz"]
    return min(archives, key=lambda c: (_rank(c), c.item_id)).item_id if archives else None


def _rank(chapter: ChapterSpec) -> float:
    try:
        return float(chapter.number) if chapter.number else float("inf")
    except ValueError:
        return float("inf")


def _has_cover(root: str | None, folder: str) -> bool:
    """A `cover.*` is already in the series folder (whatever its extension, whatever its case)."""
    if root is None:
        return False
    try:
        with os.scandir(os.path.join(root, folder)) as entries:
            return any(e.name.casefold().startswith(f"{COVER_STEM}.") for e in entries)
    except OSError:
        return False


class NativeFolderSource:
    """The `DestinationSource` of the targets of kind `native_folder`. A target of another kind is
    not its: it answers nothing for it (another source does)."""

    def __init__(
        self,
        engine: Engine,
        *,
        root_check: Callable[[Connection, str], None],
        destination: NativeFolderDestination | None = None,
    ) -> None:
        # `root_check` is the guard against the local source of Suwayomi (EF-EXP-010): it is read
        # again before every plan, because Suwayomi may take this root as its local source after
        # the binding was made. It is asked, never defaulted: no guard, no destination.
        self._engine = engine
        self._root_check = root_check
        self._cover_of: Callable[[str, str], tuple[bytes, str] | None] | None = None
        self._destination = destination or NativeFolderDestination()

    def attach(self, host: Any) -> None:
        """The host that serves the bytes also says what the cover is (its extension is the one of
        its bytes). Without one, no cover is planned: a name is never guessed."""
        self._cover_of = host.cover_image

    def _is_native(self, target_id: str) -> bool:
        with self._engine.connect() as connection:
            return (
                connection.execute(
                    sa.select(export.export_target.c.kind).where(
                        export.export_target.c.id == target_id
                    )
                ).scalar()
                == KIND
            )

    def capabilities(self, target_id: str) -> TargetCapabilities | None:
        return self._destination.capabilities if self._is_native(target_id) else None

    def renders(self, target_id: str) -> bool:
        """The host makes the ComicInfo of each CBZ this binding is served (Translator = team)."""
        return self._is_native(target_id)

    def _series(self, binding: PlanBinding) -> list[SeriesSpec]:
        with self._engine.connect() as connection:
            return native_series(connection, binding.native_formats)

    def plan(self, binding: PlanBinding) -> object | None:
        """What is missing from the folder: the executor never writes over a path that is taken
        (it refuses), so a file this binding already wrote, and a cover that is there, are not
        described again. A cover that Mihon replaced stays the one it made."""
        if not self._is_native(binding.target_id):
            return None
        self._guard(binding.destination_id)
        with self._engine.connect() as connection:
            root, recorded = self._placed(connection, binding.destination_id)
        series = self._with_covers(binding, self._series(binding), root)
        if not series:
            return None
        full = self._destination.describe(series)
        if root is None:
            return full
        kept = []
        for op in full.ops:
            ref = op.content_ref
            if op.op == "write" and ref is not None and recorded.get(ref.ref) == op.rel_path:
                if os.path.lexists(os.path.join(root, op.rel_path)):
                    continue  # written by an earlier pass and still there
            elif op.op == "write_inline" and os.path.lexists(os.path.join(root, op.rel_path)):
                continue
            kept.append(op)
        return FsOpPlan(ops=tuple(kept))

    def _with_covers(
        self, binding: PlanBinding, series: list[SeriesSpec], root: str | None
    ) -> list[SeriesSpec]:
        """The cover of a series is asked for only when none is in its folder yet (Mihon may have
        replaced it, and it is never written over), and is named by its bytes."""
        out: list[SeriesSpec] = []
        for spec in series:
            if spec.cover_item is None or _has_cover(root, series_folder(spec.name)):
                out.append(replace(spec, cover_item=None))
                continue
            image = (
                self._cover_of(binding.destination_id, spec.cover_item) if self._cover_of else None
            )
            if image is None:
                out.append(replace(spec, cover_item=None))
            else:
                out.append(replace(spec, cover_ext=image[1]))
        return out

    def _guard(self, destination_id: str) -> None:
        """Nothing is planned for a root that Suwayomi reads as its local source, nor while that
        is unknown: the pass stops with a closed code and writes nothing (`paused`)."""
        with self._engine.connect() as connection:
            root = self._root_of(connection, destination_id)
            if root is None:
                return
            try:
                self._root_check(connection, root)
            except BindingError as error:
                raise DestinationPausedError(error.code) from None

    @staticmethod
    def _root_of(connection: Connection, destination_id: str) -> str | None:
        """The folder of the binding, from the path map of its target and its sub-folder."""
        dest, target = export.shelf_destination, export.export_target
        row = connection.execute(
            sa.select(target.c.path_map, dest.c.root_subpath)
            .select_from(dest.join(target, target.c.id == dest.c.target_id))
            .where(dest.c.id == destination_id)
        ).first()
        if row is None:
            return None
        writable = [
            str(e["local"])
            for e in row.path_map or []
            if isinstance(e, dict) and e.get("mode", "rw") == "rw" and e.get("local")
        ]
        if not writable:
            return None
        base = clean_absolute(writable[0])
        return clean_absolute(f"{base}/{row.root_subpath}" if row.root_subpath else base)

    @staticmethod
    def _placed(connection: Connection, destination_id: str) -> tuple[str | None, dict[str, str]]:
        """The folder of the binding's tree, and where the manifest says its items are."""
        tree, root, indexer = export.tree, platform.physical_root, export.tree_indexer
        place = connection.execute(
            sa.select(root.c.path_entry, tree.c.root_subpath)
            .select_from(
                indexer.join(tree, tree.c.id == indexer.c.tree_id).join(
                    root, root.c.root_id == tree.c.root_id
                )
            )
            .where(indexer.c.shelf_destination_id == destination_id)
        ).first()
        if place is None:
            return None, {}
        record = export.export_record
        rows = connection.execute(
            sa.select(record.c.export_item_id, record.c.rel_path).where(
                record.c.shelf_destination_id == destination_id,
                record.c.state.in_(("written", "scanned", "resolved")),
                record.c.rel_path.is_not(None),
            )
        )
        path = (
            os.path.join(place.path_entry, place.root_subpath)
            if place.root_subpath
            else place.path_entry
        )
        return path, {r.export_item_id: r.rel_path for r in rows}

    def sidecars(self, binding: PlanBinding) -> frozenset[str]:
        """The covers the host makes for this binding: one per series that has a CBZ."""
        if not self._is_native(binding.target_id):
            return frozenset()
        return frozenset(cover_ref(s.cover_item) for s in self._series(binding) if s.cover_item)

    def calls(self, _binding: PlanBinding) -> tuple[object, ...]:
        return ()

    def sender(self, _binding: PlanBinding) -> Callable[[ApiCall], object]:
        def refuse(_call: ApiCall) -> object:
            raise ValueError("a native folder sends no call")

        return refuse
