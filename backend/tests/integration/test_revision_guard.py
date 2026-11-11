"""Suspect-revision guard, open availability, removed upstream (EF-BIB-024, EF-BIB-009)."""

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import IncomingUnit, IngestOutcome
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import storage
from tests.catalog_env import Harness, at, harness

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def item(binding, remote: str, minute: int = 0, **kw) -> IncomingUnit:  # type: ignore[no-untyped-def]
    return IncomingUnit(
        binding=binding, remote_unit_id=remote, raw_number="4", seen_at=at(minute), **kw
    )


def store_file(h: Harness, unit_id: str, variant_id: str, pages: int | None = None) -> None:
    h.connection.execute(
        sa.insert(storage.content).values(
            id=new_ulid(), unit_id=unit_id, variant_id=variant_id, kind="images",
            media_type="application/vnd.comicbook+zip", format="cbz", retention="keep",
            path="a/b.cbz", original_sha256=HASH_A, size=10, store_provenance="received",
            page_count=pages,
        )
    )  # fmt: skip


def first(h: Harness, **kw):  # type: ignore[no-untyped-def]
    binding = h.binding(h.edition())
    kw.setdefault("page_count", 20)
    kw.setdefault("revision_hint", "v1")
    kw.setdefault("content_hash", HASH_A)
    result = h.catalog.ingest(item(binding, "r1", 0, **kw))
    store_file(h, result.unit_id, result.variant_id, kw["page_count"])
    return binding, result


@pytest.mark.req("EF-BIB-024")
def test_suspect_revision_quarantined() -> None:
    with harness() as h:
        binding, base = first(h)
        again = h.catalog.ingest(
            item(binding, "r1", 5, page_count=1, revision_hint="v2", content_hash=HASH_B)
        )
        assert again.outcome is IngestOutcome.REVISION
        assert again.suspect == "fewer_pages"
        row = h.rows(
            "SELECT quarantined_at, quarantine_reason FROM variant_revision WHERE revision = 2"
        )[0]
        assert row["quarantined_at"] is not None and row["quarantine_reason"] == "fewer_pages"
        # the stored version stays exported: the content row is untouched, no replica is moved
        assert (
            h.scalar("SELECT count(*) FROM content WHERE variant_id = :v", v=base.variant_id) == 1
        )
        events = h.rows("SELECT type, payload FROM event WHERE type = 'unit.revision_suspect'")
        assert len(events) == 1  # the admin is warned


@pytest.mark.req("EF-BIB-024")
def test_suspect_revision_under_50_percent_pages_quarantined() -> None:
    with harness() as h:
        binding, _ = first(h, page_count=20)
        ok = h.catalog.ingest(item(binding, "r1", 1, page_count=10, revision_hint="v2"))
        assert ok.suspect is None  # exactly 50 %: not suspect
        bad = h.catalog.ingest(item(binding, "r1", 2, page_count=4, revision_hint="v3"))
        assert bad.suspect == "fewer_pages"  # judged against the stored 20 pages? no: see below


@pytest.mark.req("EF-BIB-024")
def test_threshold_is_adjustable_and_stubbed_is_suspect() -> None:
    with harness() as h:
        binding, _ = first(h, page_count=20)
        strict = h.catalog.ingest(
            item(binding, "r1", 1, page_count=15, revision_hint="v2"), min_page_ratio=0.9
        )
        assert strict.suspect == "fewer_pages"
        stub = h.catalog.ingest(
            item(binding, "r1", 2, page_count=20, revision_hint="v3", availability="stubbed")
        )
        assert stub.suspect == "stubbed"


@pytest.mark.req("EF-BIB-024")
def test_progressive_truncation_is_judged_against_the_stored_version() -> None:
    """Never a ratchet: 20 -> 11 pages (not suspect against 20) -> 6 pages must still be refused
    against the stored version, and a quarantined revision never becomes the new baseline."""
    with harness() as h:
        binding, _ = first(h, page_count=20)
        h.catalog.ingest(item(binding, "r1", 1, page_count=3, revision_hint="v2"))  # suspect
        again = h.catalog.ingest(item(binding, "r1", 2, page_count=4, revision_hint="v3"))
        assert again.suspect == "fewer_pages"  # 4 < 10, the baseline is still the 20 pages


@pytest.mark.req("EF-BIB-024", "EF-BIB-034")
def test_suspect_revision_never_touches_the_stored_file() -> None:
    with harness() as h:
        binding, base = first(h)
        before = h.rows("SELECT * FROM content")
        h.catalog.ingest(item(binding, "r1", 3, page_count=0, revision_hint="v2"))
        assert h.rows("SELECT * FROM content") == before
        assert h.scalar(
            "SELECT removed_at IS NULL FROM unit_variant WHERE id = :v", v=base.variant_id
        )


@pytest.mark.req("EF-BIB-009")
def test_availability_open_vocabulary() -> None:
    with harness() as h:
        binding = h.binding(h.edition())
        res = h.catalog.ingest(item(binding, "r1", 0, availability="early_access"))
        row = h.rows(
            "SELECT availability, availability_detail FROM unit WHERE id = :u", u=res.unit_id
        )
        assert row == [{"availability": "locked", "availability_detail": "early_access"}]
        h.catalog.ingest(item(binding, "r1", 1, availability="purchased"))
        row = h.rows(
            "SELECT availability, availability_detail FROM unit WHERE id = :u", u=res.unit_id
        )
        assert row == [{"availability": "purchased", "availability_detail": None}]


@pytest.mark.req("EF-BIB-009")
def test_removed_upstream_keeps_local_copy() -> None:
    with harness() as h:
        binding, base = first(h)
        h.catalog.ingest(item(binding, "r1", 2, availability="removed"))
        assert h.scalar("SELECT availability FROM unit WHERE id = :u", u=base.unit_id) == "removed"
        assert h.scalar("SELECT count(*) FROM content WHERE unit_id = :u", u=base.unit_id) == 1
        assert h.scalar("SELECT count(*) FROM unit_variant WHERE id = :v", v=base.variant_id) == 1
        # it comes back: the Unit is available again, nothing was lost in between
        h.catalog.ingest(item(binding, "r1", 3, availability="free"))
        assert h.scalar("SELECT availability FROM unit WHERE id = :u", u=base.unit_id) == "free"
        assert h.scalar("SELECT count(*) FROM content WHERE unit_id = :u", u=base.unit_id) == 1


@pytest.mark.req("EF-BIB-009")
def test_removed_upstream_with_another_live_variant_keeps_the_unit_available() -> None:
    with harness() as h:
        edition = h.edition()
        a, b = h.binding(edition), h.binding(edition)
        first_ = h.catalog.ingest(item(a, "r1", 0))
        h.catalog.ingest(item(b, "r2", 1))
        h.catalog.ingest(item(a, "r1", 2, availability="removed"))
        assert h.scalar("SELECT availability FROM unit WHERE id = :u", u=first_.unit_id) == "free"


@pytest.mark.req("EF-BIB-024")
def test_progressive_truncation_never_moves_the_baseline() -> None:
    """100 -> 51 -> 26 -> ... -> 2 pages: each step is half of the last revision, none is half of
    the version that is stored. The baseline is the stored content, not the last revision seen."""
    with harness() as h:
        binding, _ = first(h, page_count=100)
        seen = []
        for i, pages in enumerate((51, 26, 14, 7, 4, 2), start=1):
            res = h.catalog.ingest(
                item(binding, "r1", i, page_count=pages, revision_hint=f"v{i + 1}")
            )
            seen.append(res.suspect)
        assert seen == [None] + ["fewer_pages"] * 5


@pytest.mark.req("EF-BIB-024")
def test_quarantined_pages_and_hash_are_never_inherited() -> None:
    with harness() as h:
        binding, _ = first(h, page_count=20)
        bad = h.catalog.ingest(
            item(binding, "r1", 1, page_count=1, revision_hint="v2", content_hash=HASH_B)
        )
        assert bad.suspect == "fewer_pages"
        # the source then only bumps its hint: nothing proves it is whole, the quarantine stays
        nxt = h.catalog.ingest(item(binding, "r1", 2, revision_hint="v3"))
        assert nxt.suspect == "fewer_pages"
        rows = h.rows(
            "SELECT revision, page_count, content_hash, quarantine_reason "
            "FROM variant_revision ORDER BY revision"
        )
        assert rows[2]["page_count"] == 20 and rows[2]["content_hash"] == HASH_A  # the healthy ones
        assert rows[2]["quarantine_reason"] == "fewer_pages"
        # a revision that carries a page count in line with the stored version is healthy again
        ok = h.catalog.ingest(item(binding, "r1", 3, page_count=19, revision_hint="v4"))
        assert ok.suspect is None
