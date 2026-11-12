"""The queue of pending revisions: `revisions list|apply` (EF-BIB-043, EF-BIB-024).

A revision of a Variant is recorded without replacing the exported file. Those recorded before M2
are never applied by themselves, and none is applied in bulk without a confirmation:

- a revision that changes only a metadata field the Kavita API can take is pushed there, with no
  file touched (`metadata_candidates`, then `settle_metadata`); one the API cannot take (a third
  party locked the field) joins the rewrite queue;
- the others wait in the "to rewrite" queue. `report` shows them with the KOReader impact (how
  many files already read would start again) and a batch token; `apply_rewrites` needs that token
  typed back, so a queue that moved since it was read is never confirmed blind. Units already
  read are skipped unless asked for by name ("do not upgrade read Units", on by default);
- a revision the suspect-revision guard quarantined stays in the queue as `suspect` and is never
  applied, even confirmed, unless `force` comes with the grant of the reserved action. `force`
  without that grant is refused here, whatever the caller says.

`applied` means the request was accepted: the file itself is replaced by the same-path upgrade
(EF-EXP-016), under its own guards and the intent journal.
"""

import hashlib
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import sqlalchemy as sa

from shelfhold.identity.public import Allowed
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog

__all__ = [
    "ApplyReport",
    "ConfirmationError",
    "ForceRefusedError",
    "PendingRevision",
    "RevisionQueue",
    "RevisionReport",
    "RewriteRequester",
    "RewriteUnavailableError",
]


class ConfirmationError(Exception):
    """The batch token is missing or is not the queue's current one."""


class RewriteUnavailableError(Exception):
    """Nothing is connected to fetch and upgrade the new versions."""


class ForceRefusedError(Exception):
    """`force` asked without the grant of the reserved action."""


class RewriteRequester(Protocol):
    def request(
        self, connection: sa.Connection, *, unit_id: str, variant_id: str, revision: int
    ) -> bool:
        """Ask for the new version to be fetched and upgraded at the same path."""
        ...


@dataclass(frozen=True)
class PendingRevision:
    id: str
    unit_id: str
    variant_id: str
    revision: int
    nature: str
    suspect: str | None
    field: str | None
    value: str | None
    read: bool

    @property
    def koreader_reset(self) -> bool:
        """A rewrite of a file already read: KOReader would start it again."""
        return self.nature == "rewrite" and self.read


@dataclass(frozen=True)
class RevisionReport:
    items: tuple[PendingRevision, ...]
    koreader_positions: int
    token: str


@dataclass
class ApplyReport:
    requested: list[str] = field(default_factory=list)  # variant ids
    skipped_read: list[str] = field(default_factory=list)
    skipped_suspect: list[str] = field(default_factory=list)


def default_is_read(connection: sa.Connection, unit_id: str) -> bool:
    return (
        connection.execute(
            sa.text("SELECT 1 FROM reading_progress WHERE unit_id = :u AND state = 'read' LIMIT 1"),
            {"u": unit_id},
        ).first()
        is not None
    )


class RevisionQueue:
    def __init__(
        self,
        connection: sa.Connection,
        *,
        requester: RewriteRequester | None = None,
        is_read: Callable[[sa.Connection, str], bool] = default_is_read,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._c = connection
        self._requester = requester
        self._is_read = is_read
        self._now = now

    # -- the queue ------------------------------------------------------------------------

    def enqueue_missing(self) -> int:
        """Queue the revisions recorded before the queue existed (EF-BIB-043): every revision
        after the first that has no row. Their nature is `rewrite` (what changed is not known);
        nothing is applied, and a replay adds nothing."""
        r, p = catalog.variant_revision, acquisition.revision_pending
        missing = self._c.execute(
            sa.select(r.c.variant_id, r.c.revision)
            .outerjoin(p, sa.and_(p.c.variant_id == r.c.variant_id, p.c.revision == r.c.revision))
            .where(r.c.revision >= 2, p.c.id.is_(None))
            .order_by(r.c.variant_id, r.c.revision)
        ).all()
        for variant_id, revision in missing:
            self._c.execute(
                sa.insert(p).values(
                    id=new_ulid(), variant_id=variant_id, revision=revision, nature="rewrite"
                )
            )
        return len(missing)

    def _open(self) -> list[PendingRevision]:
        p, v, r = acquisition.revision_pending, catalog.unit_variant, catalog.variant_revision
        rows = self._c.execute(
            sa.select(
                p.c.id, v.c.unit_id, p.c.variant_id, p.c.revision, p.c.nature, p.c.field,
                p.c.value, r.c.quarantine_reason,
            )
            .select_from(
                p.join(v, v.c.id == p.c.variant_id).outerjoin(
                    r, sa.and_(r.c.variant_id == p.c.variant_id, r.c.revision == p.c.revision)
                )
            )
            .where(p.c.state == "open")
            .order_by(p.c.id)
        ).all()  # fmt: skip
        return [
            PendingRevision(
                id=row.id, unit_id=row.unit_id, variant_id=row.variant_id, revision=row.revision,
                nature=row.nature, suspect=row.quarantine_reason, field=row.field, value=row.value,
                read=self._is_read(self._c, row.unit_id),
            )
            for row in rows
        ]  # fmt: skip

    def report(self) -> RevisionReport:
        items = tuple(self._open())
        rewrites = sorted(i.id for i in items if i.nature == "rewrite")
        digest = hashlib.sha256("\n".join(rewrites).encode()).hexdigest()[:8]
        return RevisionReport(
            items, sum(1 for i in items if i.koreader_reset), f"rewrite-{len(rewrites)}-{digest}"
        )

    # -- applying -------------------------------------------------------------------------

    def _resolve(self, row_id: str, via: str) -> None:
        p = acquisition.revision_pending
        self._c.execute(
            sa.update(p)
            .where(p.c.id == row_id, p.c.state == "open")
            .values(state="applied", applied_via=via, resolved_at=self._now())
        )

    def metadata_candidates(self) -> list[PendingRevision]:
        """Open revisions that change only a metadata field; a suspect one is never among them."""
        return [
            i
            for i in self._open()
            if i.nature == "metadata_only" and not i.suspect and i.field and i.value
        ]

    def settle_metadata(self, row_id: str, *, taken: bool) -> None:
        """The API took the field: resolved. It could not (a lock that is not Shelfhold's): the
        revision needs the file rewritten, so it waits for a confirmed batch."""
        if taken:
            self._resolve(row_id, "kavita_api")
            return
        p = acquisition.revision_pending
        self._c.execute(sa.update(p).where(p.c.id == row_id).values(nature="rewrite"))

    def apply_rewrites(
        self,
        confirm: str | None,
        *,
        include_read: bool = False,
        force: Collection[str] = (),
        grant: Allowed | None = None,
    ) -> ApplyReport:
        """`force` names the pending revisions (their ids) whose quarantine is lifted, one by one:
        the grant of the reserved action lifts the guard for those, never for the batch."""
        if force and not isinstance(grant, Allowed):
            raise ForceRefusedError("forcing a suspect revision is a reserved action")
        named = frozenset(force)
        report = self.report()
        if not confirm or confirm != report.token:
            raise ConfirmationError(report.token)
        done = ApplyReport()
        for item in report.items:
            if item.nature != "rewrite":
                continue
            if item.suspect and item.id not in named:
                done.skipped_suspect.append(item.variant_id)
                continue
            if item.read and not include_read:
                done.skipped_read.append(item.variant_id)
                continue
            if self._requester is None:
                raise RewriteUnavailableError("no reacquisition is connected")
            if self._requester.request(
                self._c, unit_id=item.unit_id, variant_id=item.variant_id, revision=item.revision
            ):
                self._resolve(item.id, "forced_rewrite" if item.suspect else "rewrite")
                done.requested.append(item.variant_id)
        return done
