"""Shelves and their bindings to destinations (EF-EXP-002, EF-EXP-047, EF-EXP-058, EF-BIB-036).

The rules are the pure ones of `domain.bindings`; this adds what they need from the database: the
other bindings of the instance, the folder a `tree` binding writes into, the access partitioning of
the target, and the audit entry, written in the transaction that stores the change.

A shelf below 18+ is a family shelf. It is created with "retenir tant que non classé" on
(EF-EXP-050), bound only to a destination that partitions access (EF-EXP-047), lowering it returns
the removal plan (EF-BIB-037), and an Edition enters it only through the admission (EF-BIB-055),
the one rule of `RatingService`. Choosing a family shelf, changing a ceiling or the allowed
profiles are reserved actions: the caller has passed `authorize_reserved`; the audit names who.
"""

import os
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy import Connection, Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.catalog.public import Admission, RatingService, file_new_edition, track_shelf_changes
from shelfhold.export.application import fan_out
from shelfhold.export.application.komga_audit_state import (
    audit_reason,
    exposure_for,
    partitioning_of,
    remote_root_of,
)
from shelfhold.export.application.precreate import (
    AdoptedLibrary,
    Precreated,
    precreate_shelves,
)
from shelfhold.export.application.snapshot import load_context
from shelfhold.export.application.target_state import CHILD_EXPOSURE, pause_shelf
from shelfhold.export.application.withdrawal import WithdrawalPlan, build_plan
from shelfhold.export.domain.bindings import (
    PARTITIONED,
    WRITING_TRANSPORTS,
    BindingFacts,
    Egress,
    KomgaLayoutConflictError,
    KomgaPrimaryUnsupportedError,
    TargetKindTransportError,
    TreeOverlapError,
    UnpartitionedDestinationError,
    check_binding,
    check_ceiling,
    check_dropbox_folder,
    check_family_cap,
    check_partitioning,
    effective_partitioning,
    is_family,
    resolve_egress,
)
from shelfhold.export.domain.naming import (
    DEFAULT_TEMPLATE,
    NamingTemplate,
    copy_template_into_binding,
)
from shelfhold.export.domain.path_map import (
    PathEntry,
    PathMap,
    PathMapError,
    PathMode,
    clean_absolute,
)
from shelfhold.identity.public import AuditEntry, add_directly_refusal, audit_insert
from shelfhold.infrastructure.clock import Clock, SystemClock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.infrastructure.schema.export import LINK_MODES

__all__ = ["Actor", "CeilingChange", "FileResult", "ShelfService", "TargetView", "resolve_folder"]


class Actor(Protocol):
    """Who is behind a change, for the audit (`headless.actor.Actor` is one)."""

    @property
    def actor_id(self) -> str | None: ...
    @property
    def session_id(self) -> str | None: ...
    @property
    def auth_time(self) -> datetime | None: ...


class LayoutConflictLike(Protocol):
    """A file of a tree a destination would not read as the common render wrote it (the
    `LayoutConflict` of the Komga adapter is one; the rule itself lives in the adapter)."""

    @property
    def rel_path(self) -> str: ...
    @property
    def reason(self) -> str: ...


NATIVE_FOLDER = "native_folder"  # the kind of target of EF-EXP-010
CWA = "cwa"  # the kind of target of EF-EXP-022: a drop box, bound only as `dropbox`
KOBO = "kobo"  # the kind of target of EF-EXP-021: a folder a Kobo reads, bound only as `tree`
KOMGA = "komga"  # the kind of target of EF-EXP-020: an indexer, never the writer of a tree (M5)
LOCAL_LIBRARY = "local"  # the one remote library of a folder destination: no server, a folder
TAKEN = "remote_library_taken"  # the reason `FileResult` gives for the one-per-library rule


@dataclass(frozen=True)
class CeilingChange:
    shelf_id: str
    previous: int | None
    new: int | None
    withdrawals: WithdrawalPlan  # what no longer fits: a plan, nothing has moved


@dataclass(frozen=True)
class TargetView:
    """A server a shelf can be bound to: its name, its kind, what it keeps apart."""

    id: str
    name: str
    kind: str
    partitioning: str


@dataclass(frozen=True)
class FileResult:
    filed: bool
    reason: str | None = None  # the `Admission` value that refused it


class ShelfService:
    def __init__(
        self,
        engine: Engine,
        *,
        clock: Clock | None = None,
        rating: RatingService | None = None,
        root_check: Callable[[Connection, str], None] | None = None,
        on_audience_change: Callable[[], None] | None = None,
        layout_check: Callable[[Sequence[str]], Sequence[LayoutConflictLike]] | None = None,
    ) -> None:
        # who a shelf is open to changed (ceiling, profiles): the relations that the new audience
        # forbids are withdrawn from Kavita now, not at the next export (M4 L19)
        self._on_audience_change = on_audience_change
        # EF-EXP-010: the guard that a native folder root never meets the local source of Suwayomi
        # (`acquisition` owns it; the composition root hands it over, export never imports it)
        self._root_check = root_check
        # EF-EXP-020 (ADR-0052): where the files of a tree would not be read by Komga as the
        # common render writes them. The adapter describes the rule; the composition root hands
        # it over (application never imports a target).
        self._layout_check = layout_check
        self._engine = engine
        self._clock = clock or SystemClock()
        self._rating = rating or RatingService()

    def _audit(self, connection: Connection, actor: Actor | None, action: str, target: str) -> None:
        connection.execute(
            audit_insert(
                AuditEntry(
                    actor_id=actor.actor_id if actor else None, action=action,
                    at=self._clock.now(), target=target,
                    session_id=actor.session_id if actor else None,
                    auth_time=actor.auth_time if actor else None,
                )
            )
        )  # fmt: skip

    def create_shelf(
        self,
        *,
        name: str,
        phrase: str,
        max_age_rating: int | None = None,
        retention_mode: str = "passthrough",
        hold_until_rated: bool | None = None,
        allowed_profiles: tuple[str, ...] = (),
        actor: Actor | None = None,
        skip_read_upgrades: bool = True,
    ) -> str:
        """A ceiling off the scale is refused before anything is stored. "Retenir tant que non
        classé" is on by default for a shelf below 18+ (EF-EXP-050)."""
        check_ceiling(max_age_rating)
        check_family_cap(name, max_age_rating)
        hold = is_family(max_age_rating) if hold_until_rated is None else hold_until_rated
        shelf_id = new_ulid()
        with write_transaction(self._engine) as connection:
            connection.execute(
                sa.insert(export.shelf).values(
                    id=shelf_id, name=name, phrase=phrase, max_age_rating=max_age_rating,
                    retention_mode=retention_mode, hold_until_rated=hold,
                    allowed_profiles=list(allowed_profiles), skip_read_upgrades=skip_read_upgrades,
                )
            )  # fmt: skip
            self._audit(connection, actor, "shelf.create", f"shelf:{shelf_id};max:{max_age_rating}")
        return shelf_id

    def set_max_age(
        self, shelf_id: str, max_age_rating: int | None, *, actor: Actor | None = None
    ) -> CeilingChange:
        """Change the ceiling. Below 18+ the destinations of the shelf must partition access; what
        the new ceiling no longer admits comes back as a removal plan, never as a removal."""
        check_ceiling(max_age_rating)
        with write_transaction(self._engine) as connection:
            name, previous = connection.execute(
                sa.select(export.shelf.c.name, export.shelf.c.max_age_rating).where(
                    export.shelf.c.id == shelf_id
                )
            ).one()
            check_family_cap(name, max_age_rating)
            for partitioning in self._partitionings(connection, shelf_id, max_age_rating):
                check_partitioning(max_age_rating, partitioning)
            connection.execute(
                sa.update(export.shelf)
                .where(export.shelf.c.id == shelf_id)
                .values(max_age_rating=max_age_rating)
            )
            if previous != max_age_rating and self._is_linked(connection, shelf_id):
                # who may read the library depends on the ceiling: the shelf waits for a
                # `target recheck` whose audit uses the new one, whichever way it moved
                pause_shelf(connection, shelf_id, CHILD_EXPOSURE)
            self._audit(
                connection,
                actor,
                "shelf.set_ceiling",
                f"shelf:{shelf_id};{previous}->{max_age_rating}",
            )
            plan = build_plan(connection, self._rating, shelf_id)
        if previous != max_age_rating:
            self._audience_changed()
        return CeilingChange(shelf_id, previous, max_age_rating, plan)

    def set_allowed_profiles(
        self, shelf_id: str, profiles: list[str], *, actor: Actor | None = None
    ) -> list[str]:
        clean = sorted(set(profiles))
        with write_transaction(self._engine) as connection:
            with track_shelf_changes(connection, shelf_ids=[shelf_id]):  # `?since=` removals
                done = connection.execute(
                    sa.update(export.shelf)
                    .where(export.shelf.c.id == shelf_id)
                    .values(allowed_profiles=clean)
                )
            if done.rowcount != 1:
                raise LookupError(f"no such shelf: {shelf_id}")
            self._audit(connection, actor, "shelf.set_profiles", f"shelf:{shelf_id};{len(clean)}")
        self._audience_changed()
        return clean

    def _audience_changed(self) -> None:
        if self._on_audience_change is not None:
            self._on_audience_change()  # after the commit: the job reads what was written

    def set_skip_read_upgrades(
        self, shelf_id: str, skip: bool, *, actor: Actor | None = None
    ) -> None:
        """ "Do not upgrade read Units" (EF-EXP-016): on by default; `SamePathUpgrade` reads it
        for every reason it is asked to replace a file."""
        with write_transaction(self._engine) as connection:
            done = connection.execute(
                sa.update(export.shelf)
                .where(export.shelf.c.id == shelf_id)
                .values(skip_read_upgrades=skip)
            )
            if done.rowcount != 1:
                raise LookupError(f"no such shelf: {shelf_id}")
            self._audit(
                connection, actor, "shelf.set_skip_read_upgrades", f"shelf:{shelf_id};{skip}"
            )

    def set_link_mode(self, shelf_id: str, mode: str, *, actor: Actor | None = None) -> None:
        """How a kept copy reaches /store from the library (EF-EXP-015): `copy` (the default, an
        independent copy) or `hardlink` (one inode under two names, for content that can be had
        again, on one mount only). Never a symbolic link: anything else is refused before the
        database is asked. This is the only writer of `shelf.link_mode`; the decision of the Unit
        (irreplaceable: never) and of the mount is taken where the copy is made."""
        if mode not in LINK_MODES:
            raise ValueError(f"link mode {mode!r}: one of {', '.join(LINK_MODES)}")
        with write_transaction(self._engine) as connection:
            done = connection.execute(
                sa.update(export.shelf).where(export.shelf.c.id == shelf_id).values(link_mode=mode)
            )
            if done.rowcount != 1:
                raise LookupError(f"no such shelf: {shelf_id}")
            self._audit(connection, actor, "shelf.set_link_mode", f"shelf:{shelf_id};{mode}")

    def set_purge_upstream(
        self, shelf_id: str, enabled: bool, *, actor: Actor | None = None
    ) -> None:
        """Let (or no longer let) the connected service go of the downloads this shelf has filed
        and exported (EF-SRC-009, B6). Off by default. This is the **only** writer of
        `purge_upstream_after_export`, and the caller holds the reserved action
        `shelf.purge_upstream`: the nightly deed that erases upstream acts only where an admin,
        fresh or on the host, said so. Even then it is subject to "Mettre à l'abri" (EF-BIB-038)."""
        with write_transaction(self._engine) as connection:
            done = connection.execute(
                sa.update(export.shelf)
                .where(export.shelf.c.id == shelf_id)
                .values(purge_upstream_after_export=enabled)
            )
            if done.rowcount != 1:
                raise LookupError(f"no such shelf: {shelf_id}")
            self._audit(
                connection, actor, "shelf.set_purge_upstream", f"shelf:{shelf_id};{enabled}"
            )

    def shelf_id_of(self, name_or_id: str) -> str:
        """A shelf by its identifier or its name; `LookupError` when there is no such one or the
        name is ambiguous."""
        with self._engine.connect() as connection:
            found = list(
                connection.execute(
                    sa.select(export.shelf.c.id).where(
                        sa.or_(export.shelf.c.id == name_or_id, export.shelf.c.name == name_or_id)
                    )
                ).scalars()
            )
        if len(found) != 1:
            raise LookupError(f"no single shelf is called {name_or_id!r}")
        return str(found[0])

    def precreate(self, adopted: Sequence[AdoptedLibrary] = ()) -> Precreated:
        """The shelves of a new instance and one per adopted library, none rewritten."""
        return precreate_shelves(self._engine, adopted=adopted)

    def _immediate(self) -> Engine:
        """The write starts with the lock (SQLite `BEGIN IMMEDIATE`): what a filing checks is
        still true when it writes, even with another writer at the same time."""
        return self._engine.execution_options(shelfhold_begin="BEGIN IMMEDIATE")

    @staticmethod
    def _library_taken(connection: Connection, edition_id: str, shelf_id: str) -> bool:
        """One Edition, one shelf per remote library (EF-EXP-002): a library the shelf aims at
        already holds a series of this Edition made by another shelf."""
        link, dest = export.shelf_destination_remote_library, export.shelf_destination
        series, made = export.export_series, export.edition_export_series
        mine = (
            sa.select(link.c.remote_library_id)
            .select_from(link.join(dest, dest.c.id == link.c.shelf_destination_id))
            .where(dest.c.shelf_id == shelf_id)
        )
        owner = dest.alias("owner")
        elsewhere = connection.execute(
            sa.select(owner.c.shelf_id)
            .select_from(
                made.join(series, series.c.id == made.c.export_series_id).join(
                    owner, owner.c.id == series.c.shelf_destination_id
                )
            )
            .where(
                made.c.edition_id == edition_id,
                made.c.remote_library_id.in_(mine),
                owner.c.shelf_id != shelf_id,
            )
        ).first()
        return elsewhere is not None

    def _file(self, connection: Connection, edition_id: str, shelf_id: str) -> FileResult:
        """The admission every road uses, in the caller's transaction (nothing is audited here)."""
        if self._library_taken(connection, edition_id, shelf_id):
            return FileResult(False, TAKEN)
        verdict = self._rating.admission_of(connection, edition_id, shelf_id=shelf_id)
        if verdict is not Admission.ALLOWED:
            return FileResult(False, verdict.value)
        with track_shelf_changes(connection, edition_ids=[edition_id]):
            connection.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == edition_id)
                .values(shelf_id=shelf_id)
            )
        return FileResult(True)

    def file_edition(
        self, edition_id: str, shelf_id: str, *, actor: Actor | None = None
    ) -> FileResult:
        """Put an Edition on a shelf, by the one admission every road uses (EF-BIB-055): a shelf
        below 18+ takes it only once an admin rated it and nothing suggests more. A shelf that aims
        at a remote library already holding the Edition under another shelf refuses it."""
        with write_transaction(self._engine) as connection:
            result = self._file(connection, edition_id, shelf_id)
            if result.filed:
                self._audit(
                    connection, actor, "edition.file", f"edition:{edition_id};shelf:{shelf_id}"
                )
        return result

    @staticmethod
    def _family_involved(connection: Connection, edition_id: str, shelf_id: str) -> bool | None:
        """Does filing this Edition on this shelf touch a family shelf, as the target or as the
        shelf it leaves? None: no such Edition or shelf."""
        shelf = export.shelf
        target = connection.execute(
            sa.select(shelf.c.max_age_rating).where(shelf.c.id == shelf_id)
        ).first()
        current = connection.execute(
            sa.select(shelf.c.max_age_rating)
            .select_from(catalog.edition.outerjoin(shelf, shelf.c.id == catalog.edition.c.shelf_id))
            .where(catalog.edition.c.id == edition_id)
        ).first()
        if target is None or current is None:
            return None
        return is_family(target[0]) or (current[0] is not None and is_family(current[0]))

    def touches_family(self, edition_id: str, shelf_id: str) -> bool:
        """The door of the route: a family shelf, going in or out, is `shelf.choose_family`'s.
        `LookupError`: no such Edition or shelf."""
        with self._immediate().connect() as connection:
            touched = self._family_involved(connection, edition_id, shelf_id)
        if touched is None:
            raise LookupError("no such Edition or shelf")
        return touched

    def add_directly(
        self,
        edition_id: str,
        shelf_id: str,
        *,
        role: str,
        can_rate: bool,
        actor: Actor | None = None,
    ) -> FileResult:
        """ "Ajouter directement" by a person (EF-BIB-055, M3 L09): the row of the truth table
        first (`add_directly_refusal`: an Edition nobody rated never goes to a family shelf, and an
        adult without the right to classify does not choose one), then the admission every road
        uses. A refusal is audited, with the Edition and the shelf; the Edition stays where it was
        (unrated: unfiled, waiting in "À classer"). `LookupError`: no such Edition or shelf."""
        with write_transaction(self._engine) as connection:
            family = self._family_involved(connection, edition_id, shelf_id)
            rated = connection.execute(
                sa.select(catalog.edition.c.rated_at).where(catalog.edition.c.id == edition_id)
            ).first()
            if family is None or rated is None:
                raise LookupError("no such Edition or shelf")
            reason = add_directly_refusal(
                role,
                can_rate=can_rate,
                edition_rated=rated[0] is not None,
                shelf_family=family,
            )  # a family shelf, going in or out: the adult without the right does not move it
            result = (
                FileResult(False, reason)
                if reason
                else self._file(connection, edition_id, shelf_id)
            )
            target = f"edition:{edition_id};shelf:{shelf_id}"
            action = "edition.add_directly" if result.filed else "edition.add_directly_refused"
            self._audit(connection, actor, action, target)
        return result

    def transaction(self) -> AbstractContextManager[Connection]:
        """A write transaction, for a caller that rates and files in one (an approval, L35)."""
        return write_transaction(self._engine)

    def file_in(
        self, connection: Connection, edition_id: str, shelf_id: str | None, *,
        actor: Actor | None = None,
    ) -> tuple[FileResult, str | None]:  # fmt: skip
        """File an Edition in the caller's transaction, by the one admission every road uses. With
        no `shelf_id`, the shelf the kind proposes, if it takes the Edition; the result then says
        `no_default_shelf` and the Edition stays where it is (to shelve). Returns the result and
        the shelf that was meant."""
        if shelf_id is None:
            shelf_id = self._default_shelf(connection, edition_id)
            if (
                shelf_id is None
                or self._rating.admission_of(connection, edition_id, shelf_id=shelf_id)
                is not Admission.ALLOWED
            ):
                return FileResult(False, "no_default_shelf"), None
        result = self._file(connection, edition_id, shelf_id)
        if result.filed:
            self._audit(connection, actor, "edition.file", f"edition:{edition_id};shelf:{shelf_id}")
        return result, shelf_id

    def file_default(self, edition_id: str, *, actor: Actor | None = None) -> FileResult:
        """File an Edition that sits nowhere on the shelf its kind proposes. The door is the one of
        the new Edition (`file_new_edition`): a shelf below 18+ never takes an Edition nobody
        rated or rated above its ceiling, and the default never moves an Edition already shelved."""
        with write_transaction(self._engine) as connection:
            current = connection.execute(
                sa.select(catalog.edition.c.shelf_id).where(catalog.edition.c.id == edition_id)
            ).scalar_one()
            if current is not None:
                return FileResult(False, "already_shelved")
            shelf_id = self._default_shelf(connection, edition_id)
            if shelf_id is None:
                return FileResult(False, "no_default_shelf")
            if self._library_taken(connection, edition_id, shelf_id):
                return FileResult(False, TAKEN)
            filing = file_new_edition(connection, edition_id, shelf_id, rating=self._rating)
            if filing.shelf_id is None or filing.waiting:
                return FileResult(False, filing.reason)
            self._audit(connection, actor, "edition.file", f"edition:{edition_id};shelf:{shelf_id}")
        return FileResult(True)

    @staticmethod
    def _default_shelf(connection: Connection, edition_id: str) -> str | None:
        kind, work, edition, shelf = catalog.work_kind, catalog.work, catalog.edition, export.shelf
        found = connection.execute(
            sa.select(shelf.c.id)
            .select_from(
                edition.join(work, work.c.id == edition.c.work_id)
                .join(kind, kind.c.key == work.c.kind)
                .join(shelf, shelf.c.name == kind.c.default_shelf)
            )
            .where(edition.c.id == edition_id)
        ).scalar()
        return None if found is None else str(found)

    def default_shelf_of_kind(self, kind: str) -> str | None:
        """The shelf a kind of Work proposes (`work_kind.default_shelf`, a shelf name), before any
        Work exists: what a result not yet acquired will be filed in. Admission is not asked."""
        with self._engine.connect() as connection:
            found = connection.execute(
                sa.select(export.shelf.c.id)
                .select_from(
                    catalog.work_kind.join(
                        export.shelf, export.shelf.c.name == catalog.work_kind.c.default_shelf
                    )
                )
                .where(catalog.work_kind.c.key == kind)
            ).scalar()
        return None if found is None else str(found)

    def proposed_shelf(self, edition_id: str) -> str | None:
        """The shelf the kind of the Work proposes (`work_kind.default_shelf`, a shelf name), unless
        it is a shelf below 18+ that would not take the Edition (EF-EXP-002)."""
        with self._engine.connect() as connection:
            shelf_id = self._default_shelf(connection, edition_id)
            if shelf_id is None:
                return None
            verdict = self._rating.admission_of(connection, edition_id, shelf_id=shelf_id)
        return shelf_id if verdict is Admission.ALLOWED else None

    @staticmethod
    def _is_linked(connection: Connection, shelf_id: str) -> bool:
        link, destination = export.shelf_destination_remote_library, export.shelf_destination
        return (
            connection.execute(
                sa.select(link.c.shelf_destination_id)
                .select_from(
                    link.join(destination, destination.c.id == link.c.shelf_destination_id)
                )
                .where(destination.c.shelf_id == shelf_id)
            ).first()
            is not None
        )

    @staticmethod
    def _partitionings(
        connection: Connection, shelf_id: str, ceiling: str | int | None = "stored"
    ) -> list[str]:
        """The partitioning of each binding; for the ceiling the shelf is about to have, when one
        is given (`set_max_age` checks the new one, not the one in the base)."""
        target, destination, shelf = export.export_target, export.shelf_destination, export.shelf
        return [
            partitioning_of(
                connection,
                target_id=r.target_id,
                kind=r.kind,
                stored=r.access_partitioning,
                attested=r.access_partitioning_attested_at is not None,
                path_map=r.path_map,
                root_subpath=r.root_subpath,
                ceiling=r.max_age_rating if ceiling == "stored" else ceiling,  # type: ignore[arg-type]
            )
            for r in connection.execute(
                sa.select(
                    destination.c.target_id,
                    target.c.kind,
                    target.c.access_partitioning,
                    target.c.access_partitioning_attested_at,
                    target.c.path_map,
                    destination.c.root_subpath,
                    shelf.c.max_age_rating,
                )
                .select_from(
                    destination.join(target, target.c.id == destination.c.target_id).join(
                        shelf, shelf.c.id == destination.c.shelf_id
                    )
                )
                .where(destination.c.shelf_id == shelf_id)
            )
        ]

    def bind_destination(
        self,
        *,
        shelf_id: str,
        target_id: str,
        transport: str,
        plugin_egress: Egress,
        attested_egress: Egress | None = None,
        root_subpath: str = "",
        library_type: str = "manga",
        default: NamingTemplate = DEFAULT_TEMPLATE,
        attach_to_tree: str | None = None,
        actor: Actor | None = None,
    ) -> str:
        """Link a shelf to a destination (a shelf takes as many as it has folders and servers);
        the egress is what the instance attests, else the plugin's default. Refused: a shelf below
        18+ on a destination that does not partition access (looked at for this binding, whatever
        the others are), overlapping `tree` roots, one remote folder for two shelves, a `pull`
        without a readable copy. The naming template of the default is copied into the binding
        (EF-EXP-004): a later default is only proposed, the binding keeps its own copy.

        A folder that already is a tree is taken only on purpose: `attach_to_tree` names it, the
        binding becomes an indexer of that tree, and it is the folder of that tree, of the shelf
        of its primary, that it must be (ADR-0052, T1 and T2). The binding and its tree are one
        transaction."""
        destination_id = new_ulid()
        template = copy_template_into_binding(default)
        with write_transaction(self._engine) as connection:
            shelf = connection.execute(
                sa.select(export.shelf.c.retention_mode, export.shelf.c.max_age_rating).where(
                    export.shelf.c.id == shelf_id
                )
            ).one()
            row = connection.execute(
                sa.select(
                    export.export_target.c.path_map,
                    export.export_target.c.kind,
                    export.export_target.c.access_partitioning,
                    export.export_target.c.access_partitioning_attested_at,
                ).where(export.export_target.c.id == target_id)
            ).one()
            partitioning = partitioning_of(
                connection,
                target_id=target_id,
                kind=row.kind,
                stored=row.access_partitioning,
                attested=row.access_partitioning_attested_at is not None,
                path_map=row.path_map,
                root_subpath=root_subpath,
                ceiling=shelf.max_age_rating,
            )
            if row.kind == "komga" and (
                exposed := exposure_for(
                    connection,
                    target_id,
                    remote_root_of(row.path_map, root_subpath),
                    shelf.max_age_rating,
                )
            ):  # whatever the shelf: the last audit saw a child reach this folder (#320 M6)
                raise UnpartitionedDestinationError(
                    f"the last audit of this Komga saw a child account reach it ({exposed})",
                    reason=exposed,
                )
            check_partitioning(
                shelf.max_age_rating,
                partitioning,
                reason=(
                    audit_reason(connection, target_id)
                    if row.kind == "komga" and partitioning not in PARTITIONED
                    else None
                ),
            )
            if row.kind == CWA and transport != "dropbox":
                raise TargetKindTransportError("a CWA drop box is bound as a drop box")
            if row.kind == KOBO and transport != "tree":
                # the sync surface of the device (its `pull` binding) comes with its own lot, on
                # the same declaration; a folder a reader mounts is a tree, never anything else
                raise TargetKindTransportError("a Kobo folder is bound as a tree")
            if row.kind == KOMGA and attach_to_tree is None:
                # J-SIMP-4: the executor plans no write for a Komga tree, so a binding that owns
                # one would leave the shelf without its files. Until M5, Komga is bound as an
                # indexer of a tree another binding writes (ADR-0052).
                raise KomgaPrimaryUnsupportedError(
                    "Komga is bound as a secondary destination only until M5: attach it to a tree"
                )
            if row.kind == KOMGA and attach_to_tree is not None and self._layout_check is not None:
                # ADR-0052: Komga reads the folder as the series. A tree whose files the common
                # render would not make it see that way is refused, never rewritten (files are
                # frozen): the indexer is told of the conflict, not of a repair.
                placed = fan_out.tree_place(connection, attach_to_tree)
                rel_paths = (
                    connection.execute(
                        sa.select(export.export_record.c.rel_path).where(
                            export.export_record.c.shelf_destination_id == placed[0],
                            export.export_record.c.rel_path.is_not(None),
                        )
                    )
                    .scalars()
                    .all()
                    if placed is not None
                    else []
                )
                conflicts = self._layout_check(rel_paths)
                if conflicts:
                    raise KomgaLayoutConflictError(
                        f"the tree holds {len(conflicts)} file(s) Komga would not read"
                        f" as the render does ({conflicts[0].reason})",
                        reason=conflicts[0].reason,
                    )
            path_map = PathMap(
                tuple(
                    PathEntry(e["local"], e["remote"], PathMode(e.get("mode", "rw")))
                    for e in row.path_map
                )
            )
            entries = path_map.writable()
            declared = (
                os.path.realpath(clean_absolute(entries[0].local))
                if row.kind == CWA and entries
                else None
            )
            if declared is not None:
                check_dropbox_folder(declared, root_subpath, None)  # before a `..` is resolved
            root = None
            if transport in WRITING_TRANSPORTS and entries:
                root = clean_absolute(
                    f"{entries[0].local}/{root_subpath}" if root_subpath else entries[0].local
                )
            real, identity = resolve_folder(root)
            if declared is not None:
                check_dropbox_folder(declared, root_subpath, real)
            new = BindingFacts(
                destination_id, shelf_id, target_id, transport,
                resolve_egress(plugin_default=plugin_egress, attested=attested_egress), real,
                identity, _remote(path_map, root),
            )  # fmt: skip
            others = self._existing(connection)
            if attach_to_tree is not None:
                others = self._without_tree(connection, others, attach_to_tree, shelf_id, real)
            check_binding(new, others, retention_mode=shelf.retention_mode)
            if row.kind == NATIVE_FOLDER and real is not None and self._root_check is not None:
                self._root_check(connection, real)
            try:
                with connection.begin_nested():
                    connection.execute(
                        sa.insert(export.shelf_destination).values(
                            id=destination_id, shelf_id=shelf_id, target_id=target_id,
                            transport=transport, egress=new.egress, root_subpath=root_subpath,
                            template=template.text, template_origin=template.origin,
                            kavita_library_type=library_type,
                        )
                    )  # fmt: skip
            except sa.exc.IntegrityError:
                if attach_to_tree is None:
                    raise
                raise TreeOverlapError("this server already indexes this folder") from None
            if row.kind == NATIVE_FOLDER:
                self._register_local_library(connection, destination_id, target_id)
            if transport == "tree":
                if attach_to_tree is None:
                    fan_out.ensure_tree(
                        connection, destination_id=destination_id, target_id=target_id,
                        local_root=real,
                    )  # fmt: skip
                else:
                    fan_out.attach_secondary(
                        connection, tree_id=attach_to_tree, destination_id=destination_id,
                        target_id=target_id,
                    )  # fmt: skip
            self._audit(connection, actor, "shelf.bind", f"{shelf_id}|{destination_id}")
        return destination_id

    @staticmethod
    def _register_local_library(
        connection: Connection, destination_id: str, target_id: str
    ) -> None:
        """A folder destination has one remote library, its own and local (M1a: an exported series
        needs a `remote_library`; nothing is created on any server): the same row for every
        binding of the target, linked to this binding."""
        library = export.remote_library
        connection.execute(
            sqlite_insert(library)
            .values(id=new_ulid(), export_target_id=target_id, remote_ref=LOCAL_LIBRARY)
            .on_conflict_do_nothing(index_elements=["export_target_id", "remote_ref"])
        )
        library_id = connection.execute(
            sa.select(library.c.id).where(
                library.c.export_target_id == target_id, library.c.remote_ref == LOCAL_LIBRARY
            )
        ).scalar_one()
        connection.execute(
            sa.insert(export.shelf_destination_remote_library).values(
                shelf_destination_id=destination_id,
                remote_library_id=library_id,
                export_target_id=target_id,
            )
        )

    @staticmethod
    def _without_tree(
        connection: Connection,
        existing: list[BindingFacts],
        tree_id: str,
        shelf_id: str,
        root: str | None,
    ) -> list[BindingFacts]:
        """The bindings that overlap are the ones of the tree itself, which the explicit choice
        takes in: the tree must be one of this shelf, and the folder the very one of the tree."""
        placed = fan_out.tree_place(connection, tree_id)
        if placed is None or placed[2] != shelf_id:
            raise TreeOverlapError("a tree is attached to by the shelf of its primary only")
        found = None if root is None else fan_out.locate_root(connection, root)
        if found is None or found != (placed[0], placed[1]):
            raise TreeOverlapError("the folder is not the one of the tree")
        mine = set(
            connection.execute(
                sa.select(export.tree_indexer.c.shelf_destination_id).where(
                    export.tree_indexer.c.tree_id == tree_id
                )
            ).scalars()
        )
        return [b for b in existing if b.shelf_destination_id not in mine]

    def list_bindings(self, shelf_id: str) -> list[fan_out.BindingView]:
        """The bindings of a shelf with their tree and their role. A binding made before trees
        existed gets its tree here, once (idempotent), from the folder it writes: a write, for the
        callers that are one (the pass, the tests). A read route uses `read_bindings`."""
        with write_transaction(self._engine) as connection:
            self._require_shelf(connection, shelf_id)
            return fan_out.ensure_trees(connection, shelf_id)

    def read_bindings(self, shelf_id: str) -> list[fan_out.BindingView]:
        """The same, as the database holds it: nothing is written (a safe method writes nothing)."""
        with self._engine.connect() as connection:
            self._require_shelf(connection, shelf_id)
            return fan_out.bindings_of(connection, shelf_id)

    def list_targets(self) -> list[TargetView]:
        """The servers a shelf can be bound to, with what each keeps apart (the value the barrier
        of a family shelf reads). Nothing of the config, no key."""
        target = export.export_target
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    target.c.id,
                    target.c.name,
                    target.c.kind,
                    target.c.access_partitioning,
                    target.c.access_partitioning_attested_at,
                ).order_by(target.c.name, target.c.id)
            ).all()
        return [
            TargetView(
                row.id, row.name, row.kind,
                effective_partitioning(
                    row.kind, row.access_partitioning,
                    attested=row.access_partitioning_attested_at is not None,
                ),
            )
            for row in rows
        ]  # fmt: skip

    @staticmethod
    def _require_shelf(connection: Connection, shelf_id: str) -> None:
        if (
            connection.execute(
                sa.select(export.shelf.c.id).where(export.shelf.c.id == shelf_id)
            ).first()
            is None
        ):
            raise LookupError(shelf_id)

    def pause_family_of_target(self, target_id: str) -> list[str]:
        """A new path map moves the folders a Kavita library reads, so who can read what has to be
        audited again: every linked shelf below 18+ of the target waits for a `target recheck`."""
        paused = []
        with write_transaction(self._engine) as connection:
            rows = connection.execute(
                sa.select(export.shelf.c.id, export.shelf.c.max_age_rating)
                .select_from(
                    export.shelf.join(
                        export.shelf_destination,
                        export.shelf_destination.c.shelf_id == export.shelf.c.id,
                    )
                )
                .where(export.shelf_destination.c.target_id == target_id)
                .distinct()
            ).all()
            for row in rows:
                linked = is_family(row.max_age_rating) and self._is_linked(connection, row.id)
                if linked and pause_shelf(connection, row.id, CHILD_EXPOSURE):
                    paused.append(row.id)
        return paused

    def check_path_map(self, target_id: str, entries: list[dict[str, str]]) -> None:
        """A new path map of a target moves the folders of its bindings: they must still not
        overlap another binding's, nor share a remote folder (the same rules as binding)."""
        path_map = PathMap(
            tuple(
                PathEntry(e["local"], e["remote"], PathMode(e.get("mode", "rw"))) for e in entries
            )
        )
        writable = path_map.writable()
        with self._engine.connect() as connection:
            kind = connection.execute(
                sa.select(export.export_target.c.kind).where(export.export_target.c.id == target_id)
            ).scalar()
            if kind == NATIVE_FOLDER and self._root_check is not None:
                for (
                    entry
                ) in writable:  # a root moved onto the local source of Suwayomi (EF-EXP-010)
                    self._root_check(connection, entry.local)
            rows = connection.execute(
                sa.select(
                    export.shelf_destination.c.id,
                    export.shelf_destination.c.shelf_id,
                    export.shelf_destination.c.transport,
                    export.shelf_destination.c.egress,
                    export.shelf_destination.c.root_subpath,
                    export.shelf.c.retention_mode,
                )
                .select_from(
                    export.shelf_destination.join(
                        export.shelf, export.shelf.c.id == export.shelf_destination.c.shelf_id
                    )
                )
                .where(export.shelf_destination.c.target_id == target_id)
            ).all()
            others = [f for f in self._existing(connection) if f.target_id != target_id]
            for row in rows:
                root = None
                if row.transport in WRITING_TRANSPORTS and writable:
                    base = writable[0].local
                    root = clean_absolute(
                        f"{base}/{row.root_subpath}" if row.root_subpath else base
                    )
                real, identity = resolve_folder(root)
                facts = BindingFacts(
                    row.id, row.shelf_id, target_id, row.transport, row.egress, real, identity,
                    _remote(path_map, root),
                )  # fmt: skip
                check_binding(facts, others, retention_mode=row.retention_mode)
                others.append(facts)

    def _existing(self, connection: sa.Connection) -> list[BindingFacts]:
        found = []
        for row in connection.execute(sa.select(export.shelf_destination.c.id)):
            ctx = load_context(connection, row.id)
            root = ctx.root_local if ctx.transport in WRITING_TRANSPORTS else None
            real, identity = resolve_folder(root)
            found.append(
                BindingFacts(
                    ctx.destination_id, ctx.shelf_id, ctx.target_id, ctx.transport, ctx.egress,
                    real, identity, _remote(ctx.path_map, root),
                )
            )  # fmt: skip
        return found


def _remote(path_map: PathMap, root: str | None) -> str | None:
    """The folder as the server sees it, when the path map says."""
    if root is None:
        return None
    try:
        return path_map.to_remote(root)
    except PathMapError:
        return None


def resolve_folder(path: str | None) -> tuple[str | None, tuple[int, int] | None]:
    """The real path of a folder (links followed, so an alias is the same folder) and its
    `(st_dev, st_ino)` when it exists (so are a bind mount's or a case-insensitive twin)."""
    if path is None:
        return None, None
    real = os.path.realpath(path)
    try:
        observed = os.stat(real)
    except FileNotFoundError:
        return real, None
    return real, (observed.st_dev, observed.st_ino)


@dataclass(frozen=True)
class ShelfSummary:
    id: str
    name: str
    max_age_rating: int | None


def list_shelves(connection: Connection) -> list[ShelfSummary]:
    """The shelves, in name order: what `GET /shelves` lists (the admin's, M3 L11). No allowed
    profile, no path, no phrase: the list says a shelf exists and under which ceiling."""
    shelf = export.shelf.c
    rows = connection.execute(
        sa.select(shelf.id, shelf.name, shelf.max_age_rating).order_by(shelf.name, shelf.id)
    )
    return [ShelfSummary(r.id, r.name, r.max_age_rating) for r in rows]


def list_shelves_open_to(connection: Connection, user_id: str) -> list[ShelfSummary]:
    """The shelves this profile may be named (OPDS, M4 L48): a shelf whose `allowed_profiles` is
    not empty and does not name the profile is left out, as the shelf is reserved to others (a
    parental control action, `shelf.allowed_profiles`). The ceiling is the caller's to apply."""
    shelf = export.shelf.c
    rows = connection.execute(
        sa.select(shelf.id, shelf.name, shelf.max_age_rating, shelf.allowed_profiles).order_by(
            shelf.name, shelf.id
        )
    )
    return [
        ShelfSummary(r.id, r.name, r.max_age_rating)
        for r in rows
        if not r.allowed_profiles or user_id in r.allowed_profiles
    ]
