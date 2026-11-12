"""Revisions accumulated before M2 and the "to rewrite" queue (EF-BIB-043, EF-BIB-024)."""

import pytest
import sqlalchemy as sa

from shelfhold.catalog.application.revisions import (
    ConfirmationError,
    ForceRefusedError,
    RevisionQueue,
)
from shelfhold.catalog.public import IncomingUnit
from shelfhold.identity.domain.reserved import Allowed
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import storage
from tests.catalog_env import Harness, at, harness
from tests.support.revisions import apply_metadata

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


class Recorder:
    """A Kavita that takes the field, or refuses, and a requester of reacquisition."""

    def __init__(self, *, takes: bool = True, boom: Exception | None = None) -> None:
        self.pushed: list[tuple[str, str, str]] = []
        self.requested: list[str] = []
        self.takes = takes
        self.boom = boom

    def push(self, connection: sa.Connection, *, unit_id: str, field: str, value: str) -> bool:
        if self.boom is not None:
            raise self.boom
        self.pushed.append((unit_id, field, value))
        return self.takes

    def request(
        self, connection: sa.Connection, *, unit_id: str, variant_id: str, revision: int
    ) -> bool:
        self.requested.append(variant_id)
        return True


def make_item(binding, remote: str, minute: int, **kw) -> IncomingUnit:  # type: ignore[no-untyped-def]
    return IncomingUnit(
        binding=binding,
        remote_unit_id=remote,
        raw_number=remote[1:] or "1",
        seen_at=at(minute),
        **kw,
    )


def with_file(h: Harness, unit_id: str, variant_id: str) -> None:
    h.connection.execute(
        sa.insert(storage.content).values(
            id=new_ulid(), unit_id=unit_id, variant_id=variant_id, kind="images",
            media_type="application/vnd.comicbook+zip", format="cbz", retention="keep",
            path=f"{unit_id}.cbz", original_sha256=HASH_A, size=10, store_provenance="received",
        )
    )  # fmt: skip


def accumulate(h: Harness, count: int) -> list[str]:
    """`count` Variants with a stored file and a second revision recorded before M2, that is,
    without a `revision_pending` row (the history is all there is)."""
    binding = h.binding(h.edition())
    units = []
    for n in range(1, count + 1):
        first = h.catalog.ingest(
            make_item(binding, f"c{n}", 0, page_count=20, content_hash=HASH_A, revision_hint="v1")
        )
        with_file(h, first.unit_id, first.variant_id)
        units.append(first.unit_id)
        h.connection.execute(
            sa.text(
                "INSERT INTO variant_revision (variant_id, revision, seen_at, page_count, "
                "content_hash, revision_hint) VALUES (:v, 2, :t, 20, :h, 'v2')"
            ),
            {"v": first.variant_id, "t": at(1), "h": HASH_B},
        )
        h.connection.execute(
            sa.text("UPDATE unit_variant SET revision = 2 WHERE id = :v"), {"v": first.variant_id}
        )
    h.connection.execute(sa.text("DELETE FROM revision_pending"))
    return units


def queue(h: Harness, recorder: Recorder | None = None, **kw) -> RevisionQueue:  # type: ignore[no-untyped-def]
    rec = recorder or Recorder()
    return RevisionQueue(h.connection, requester=rec, **kw)


@pytest.mark.req("EF-BIB-043")
def test_pre_m2_revisions_never_bulk_rewritten() -> None:
    with harness() as h:
        accumulate(h, 400)
        before = h.rows("SELECT * FROM content")
        rec = Recorder()
        q = queue(h, rec)
        assert q.enqueue_missing() == 400
        assert q.enqueue_missing() == 0  # idempotent
        rows = h.rows("SELECT nature, state FROM revision_pending")
        assert len(rows) == 400 and {(r["nature"], r["state"]) for r in rows} == {
            ("rewrite", "open")
        }
        assert apply_metadata(q, rec, h.connection) == []  # nothing here is a metadata-only change
        assert rec.requested == [] and rec.pushed == []
        assert h.rows("SELECT * FROM content") == before  # not one file was rewritten


@pytest.mark.req("EF-BIB-043")
@pytest.mark.parametrize("confirm", [None, "", "yes", "rewrite-400-00000000"])
def test_rewrite_queue_refuses_without_the_batch_token(confirm: str | None) -> None:
    with harness() as h:
        accumulate(h, 3)
        rec = Recorder()
        q = queue(h, rec)
        q.enqueue_missing()
        with pytest.raises(ConfirmationError):
            q.apply_rewrites(confirm)
        assert rec.requested == []
        assert h.scalar("SELECT count(*) FROM revision_pending WHERE state = 'open'") == 3


@pytest.mark.req("EF-BIB-043")
def test_rewrite_queue_shows_koreader_impact_and_needs_confirmation() -> None:
    with harness() as h:
        units = accumulate(h, 5)
        read = set(units[:2])
        rec = Recorder()
        q = queue(h, rec, is_read=lambda _c, unit_id: unit_id in read)
        q.enqueue_missing()
        report = q.report()
        assert len(report.items) == 5
        assert report.koreader_positions == 2  # the files already read, whose position would go
        assert all(i.koreader_reset == (i.unit_id in read) for i in report.items)
        # "do not upgrade the Units already read" is on by default
        done = q.apply_rewrites(report.token)
        assert sorted(rec.requested) == sorted(
            i.variant_id for i in report.items if i.unit_id not in read
        )
        assert len(done.skipped_read) == 2
        assert h.scalar("SELECT count(*) FROM revision_pending WHERE state = 'open'") == 2
        # unticked on purpose, by name
        again = q.apply_rewrites(q.report().token, include_read=True)
        assert len(again.requested) == 2
        assert h.scalar("SELECT count(*) FROM revision_pending WHERE state = 'open'") == 0


@pytest.mark.req("EF-BIB-043", "EF-BIB-024")
def test_suspect_rows_need_force_and_force_needs_the_reserved_grant() -> None:
    with harness() as h:
        binding = h.binding(h.edition())
        base = h.catalog.ingest(make_item(binding, "c1", 0, page_count=20, content_hash=HASH_A))
        with_file(h, base.unit_id, base.variant_id)
        h.catalog.ingest(
            make_item(binding, "c1", 1, page_count=1, content_hash=HASH_B, revision_hint="v2")
        )
        rec = Recorder()
        q = queue(h, rec)
        report = q.report()
        assert [i.suspect for i in report.items] == ["fewer_pages"]
        done = q.apply_rewrites(report.token)  # confirmed, but the guard still holds
        assert rec.requested == [] and len(done.skipped_suspect) == 1
        with pytest.raises(ForceRefusedError):  # no reserved action behind `force`
            q.apply_rewrites(report.token, force=[i.id for i in report.items])
        with pytest.raises(ForceRefusedError):
            q.apply_rewrites(
                report.token,
                force=[i.id for i in report.items],
                grant=object(),  # type: ignore[arg-type]
            )
        assert rec.requested == []
        forced = q.apply_rewrites(report.token, force=[i.id for i in report.items], grant=Allowed())
        assert len(forced.requested) == 1


@pytest.mark.req("EF-BIB-043")
def test_metadata_only_revision_applied_via_kavita_api() -> None:
    with harness() as h:
        binding = h.binding(h.edition())
        base = h.catalog.ingest(make_item(binding, "c1", 0, page_count=20, content_hash=HASH_A))
        with_file(h, base.unit_id, base.variant_id)
        before = h.rows("SELECT * FROM content")
        h.catalog.ingest(
            make_item(
                binding,
                "c1",
                1,
                page_count=20,
                content_hash=HASH_A,
                revision_hint="v2",
                metadata=(("summary", "A new summary"),),
            )
        )
        (row,) = h.rows("SELECT nature, state, field, value FROM revision_pending")
        assert (row["nature"], row["state"]) == ("metadata_only", "open")
        rec = Recorder()
        q = queue(h, rec)
        applied = apply_metadata(q, rec, h.connection)  # no confirmation: nothing is rewritten
        assert rec.pushed == [(base.unit_id, "summary", "A new summary")]
        assert len(applied) == 1 and rec.requested == []
        (done,) = h.rows("SELECT state, applied_via, resolved_at FROM revision_pending")
        assert done["state"] == "applied" and done["applied_via"] == "kavita_api"
        assert done["resolved_at"] is not None
        assert h.rows("SELECT * FROM content") == before


@pytest.mark.req("EF-BIB-043")
def test_metadata_the_api_cannot_take_goes_to_the_rewrite_queue() -> None:
    with harness() as h:
        binding = h.binding(h.edition())
        h.catalog.ingest(make_item(binding, "c1", 0, page_count=20, content_hash=HASH_A))
        h.catalog.ingest(
            make_item(
                binding,
                "c1",
                1,
                page_count=20,
                content_hash=HASH_A,
                revision_hint="v2",
                metadata=(("summary", "x"),),
            )
        )
        takes_not = Recorder(takes=False)  # a third party locked the field
        q = queue(h, takes_not)
        assert apply_metadata(q, takes_not, h.connection) == []
        (row,) = h.rows("SELECT nature, state FROM revision_pending")
        assert (row["nature"], row["state"]) == ("rewrite", "open")


@pytest.mark.req("EF-BIB-043", "EF-BIB-024")
def test_crash_while_applying_leaves_the_row_open_and_replays_once() -> None:
    with harness() as h:
        binding = h.binding(h.edition())
        h.catalog.ingest(make_item(binding, "c1", 0, page_count=20, content_hash=HASH_A))
        h.catalog.ingest(
            make_item(
                binding,
                "c1",
                1,
                page_count=20,
                content_hash=HASH_A,
                revision_hint="v2",
                metadata=(("summary", "x"),),
            )
        )
        broken = Recorder(boom=RuntimeError("kill"))
        with pytest.raises(RuntimeError):
            apply_metadata(queue(h, broken), broken, h.connection)
        assert h.scalar("SELECT state FROM revision_pending") == "open"  # nothing marked
        good = Recorder()
        assert len(apply_metadata(queue(h, good), good, h.connection)) == 1
        assert len(apply_metadata(queue(h, good), good, h.connection)) == 0  # applied once
        assert len(good.pushed) == 1


@pytest.mark.req("EF-BIB-043")
def test_enqueue_marks_quarantined_history_as_suspect() -> None:
    with harness() as h:
        units = accumulate(h, 1)
        h.connection.execute(
            sa.text(
                "UPDATE variant_revision SET quarantined_at = :t, "
                "quarantine_reason = 'fewer_pages' WHERE revision = 2"
            ),
            {"t": at(2)},
        )
        q = queue(h)
        q.enqueue_missing()
        (item,) = q.report().items
        assert item.unit_id == units[0] and item.suspect == "fewer_pages"


@pytest.mark.req("EF-BIB-024", "EF-BIB-043")
def test_force_lifts_the_guard_for_the_named_revision_only() -> None:
    with harness() as h:
        binding = h.binding(h.edition())
        for n in ("c1", "c2"):
            base = h.catalog.ingest(make_item(binding, n, 0, page_count=20, content_hash=HASH_A))
            with_file(h, base.unit_id, base.variant_id)
            h.catalog.ingest(
                make_item(binding, n, 1, page_count=1, content_hash=HASH_B, revision_hint="v2")
            )
        rec = Recorder()
        q = queue(h, rec)
        report = q.report()
        assert len(report.items) == 2 and all(i.suspect for i in report.items)
        named = report.items[0]
        done = q.apply_rewrites(report.token, force=[named.id], grant=Allowed())
        assert done.requested == [named.variant_id]
        assert len(done.skipped_suspect) == 1  # the other one stays quarantined
        assert h.scalar("SELECT count(*) FROM revision_pending WHERE state = 'open'") == 1
