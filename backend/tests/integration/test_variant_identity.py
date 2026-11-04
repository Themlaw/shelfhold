"""Two-level identity (EF-BIB-034, EF-BIB-050, EF-BIB-049): places, variants, revisions."""

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import (
    Discriminant,
    EditionSpec,
    IncomingUnit,
    IngestOutcome,
    TextVersionRef,
    translation_discriminant,
)
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import storage
from tests.catalog_env import at, harness


def unit(binding, remote: str, raw: str | None, minute: int = 0, **kw) -> IncomingUnit:  # type: ignore[no-untyped-def]
    return IncomingUnit(
        binding=binding, remote_unit_id=remote, raw_number=raw, seen_at=at(minute), **kw
    )


@pytest.mark.req("EF-BIB-001")
def test_series_optional_above_work() -> None:
    with harness() as h:
        lone = h.work()
        series = h.catalog.create_series("Cycle")
        member = h.catalog.create_work("novel", "Tome one", series_id=series, position="1")
        assert h.catalog.series_of(lone) is None
        assert h.catalog.series_of(member) == series
        # an Edition hangs from a Work whether or not a Series exists above it
        assert h.edition(lone).work_id == lone
        assert h.edition(member, kind="novel", media="text").work_id == member


@pytest.mark.req("EF-BIB-034")
def test_unnumbered_extras_ordered_by_source_order() -> None:
    with harness() as h:
        edition = h.edition()
        binding = h.binding(edition)
        for remote, order in (("x3", 3), ("x1", 1), ("x2", 2)):
            h.catalog.ingest(unit(binding, remote, "Extra", source_order=order))
        h.catalog.ingest(unit(binding, "c1", "1", source_order=9))
        ordered = h.catalog.units_in_order(edition.id)
        assert [u.number_raw for u in ordered] == ["1", "Extra", "Extra", "Extra"]
        extras = [u for u in ordered if u.track == "unnumbered"]
        assert [u.source_order for u in extras] == [1, 2, 3]
        assert {u.number_norm for u in extras} == {""}  # no number invented


@pytest.mark.req("EF-BIB-034")
def test_tie_oldest_variant_keeps_place() -> None:
    with harness() as h:
        edition = h.edition()
        binding = h.binding(edition)
        late = h.catalog.ingest(unit(binding, "b", "10", 5, team_label="B"))
        early = h.catalog.ingest(unit(binding, "a", "10", 1, team_label="A"))
        assert early.unit_id == late.unit_id  # same place, one Unit
        assert h.catalog.place_owner(early.unit_id) == early.variant_id
        assert len(h.rows("SELECT id FROM unit_variant WHERE unit_id = :u", u=early.unit_id)) == 2


@pytest.mark.req("EF-BIB-034")
def test_same_team_republication_is_new_revision() -> None:
    with harness() as h:
        edition = h.edition()
        binding = h.binding(edition)
        first = h.catalog.ingest(unit(binding, "r1", "4", 0, team_label="A", revision_hint="v1"))
        again = h.catalog.ingest(unit(binding, "r1", "4", 9, team_label="A", revision_hint="v2"))
        assert again.outcome is IngestOutcome.REVISION
        assert again.variant_id == first.variant_id
        assert again.revision == 2
        revisions = h.rows("SELECT revision, revision_hint FROM variant_revision ORDER BY revision")
        assert [r["revision"] for r in revisions] == [1, 2]
        same = h.catalog.ingest(unit(binding, "r1", "4", 10, team_label="A", revision_hint="v2"))
        assert same.outcome is IngestOutcome.UNCHANGED
        assert len(h.rows("SELECT id FROM variant_revision")) == 2


@pytest.mark.req("EF-BIB-034")
def test_republication_before_m2_never_replaces_exported_file() -> None:
    with harness() as h:
        edition = h.edition()
        binding = h.binding(edition)
        first = h.catalog.ingest(unit(binding, "r1", "4", 0, revision_hint="v1"))
        h.connection.execute(
            sa.insert(storage.content).values(
                id=new_ulid(),
                unit_id=first.unit_id,
                variant_id=first.variant_id,
                kind="images",
                media_type="application/vnd.comicbook+zip",
                format="cbz",
                retention="keep",
                path="a/b.cbz",
                original_sha256="sha256:" + "a" * 64,
                size=10,
                store_provenance="received",
            )
        )
        before = h.rows("SELECT * FROM content")
        h.catalog.ingest(
            unit(binding, "r1", "4", 9, revision_hint="v2", content_hash="sha256:" + "b" * 64)
        )
        assert h.rows("SELECT * FROM content") == before  # the exported file is untouched
        assert h.rows("SELECT revision FROM unit_variant") == [{"revision": 2}]


@pytest.mark.req("EF-BIB-034", "EF-BIB-050")
def test_variant_identity_is_source_binding_and_remote_id() -> None:
    with harness() as h:
        edition = h.edition()
        a, b = h.binding(edition), h.binding(edition)
        first = h.catalog.ingest(unit(a, "same", "1"))
        other_binding = h.catalog.ingest(unit(b, "same", "1"))
        assert first.outcome is IngestOutcome.NEW_VARIANT
        assert other_binding.outcome is IngestOutcome.NEW_VARIANT  # the pair is the identity
        assert other_binding.variant_id != first.variant_id
        assert other_binding.unit_id == first.unit_id
        assert h.catalog.ingest(unit(a, "same", "1")).variant_id == first.variant_id
        assert len(h.rows("SELECT id FROM unit_variant")) == 2


@pytest.mark.req("EF-BIB-050")
def test_new_remote_id_never_rewrites_existing_variant() -> None:
    with harness() as h:
        edition = h.edition()
        binding = h.binding(edition)
        old = h.catalog.ingest(unit(binding, "S1-Ch1", "S1 Ch.1", 0))
        new = h.catalog.ingest(unit(binding, "id-9", "S1 Ch.1", 5))
        assert new.outcome is IngestOutcome.NUMBERING_CONFLICT
        assert new.variant_id != old.variant_id
        assert new.suggestion_id is not None
        assert h.rows(
            "SELECT remote_unit_id, revision FROM unit_variant WHERE id = :i", i=old.variant_id
        ) == [{"remote_unit_id": "S1-Ch1", "revision": 1}]
        for variant in (old, new):  # each keeps its own single, first revision
            assert (
                h.scalar(
                    "SELECT count(*) FROM variant_revision WHERE variant_id = :i",
                    i=variant.variant_id,
                )
                == 1
            )
        suggestions = h.rows("SELECT entity_type, reason, state FROM merge_suggestion")
        assert suggestions == [
            {"entity_type": "variant", "reason": "numbering_conflict", "state": "open"}
        ]
        assert h.scalar("SELECT count(*) FROM variant_revision WHERE revision > 1") == 0


@pytest.mark.req("EF-BIB-050")
def test_reupload_supersedes_without_irreplaceable_candidate() -> None:
    with harness() as h:
        edition = h.edition()
        binding = h.binding(edition)
        olds = [h.catalog.ingest(unit(binding, f"old{n}", str(n), n)) for n in range(1, 6)]
        for old in olds:
            h.catalog.mark_removed(old.variant_id, at(100))
        assert len(h.catalog.irreplaceable_candidates(edition.id)) == 5  # nothing replaces them yet
        news = [h.catalog.ingest(unit(binding, f"new{n}", str(n), 200 + n)) for n in range(1, 6)]
        assert all(n.outcome is IngestOutcome.SUPERSEDING for n in news)
        assert [n.supersedes_variant_id for n in news] == [o.variant_id for o in olds]
        assert h.catalog.irreplaceable_candidates(edition.id) == []
        assert h.scalar("SELECT count(*) FROM merge_suggestion") == 0  # no false content error


@pytest.mark.req("EF-BIB-001")
def test_two_translations_same_work_two_editions() -> None:
    with harness() as h:
        work = h.work("novel")
        c1, c2 = h.catalog.create_contributor("Alice"), h.catalog.create_contributor("Alice")
        e1 = h.edition(
            work,
            translation_type="official",
            media="text",
            text_version=TextVersionRef(contributor_id=c1),
        )
        e2 = h.edition(
            work,
            translation_type="official",
            media="text",
            text_version=TextVersionRef(contributor_id=c2),
        )
        assert e1.id != e2.id  # same displayed name, two contributors: two Editions
        again = h.edition(
            work,
            translation_type="official",
            media="text",
            text_version=TextVersionRef(contributor_id=c1),
        )
        assert again.id == e1.id
        assert not again.created
        assert e1.text_version != "" and e1.text_version != e2.text_version


@pytest.mark.req("EF-BIB-049")
def test_manga_team_stays_variant_text_version_empty() -> None:
    assert translation_discriminant("manga", "community") is Discriminant.TEAM
    assert translation_discriminant("webtoon", "unknown") is Discriminant.TEAM
    assert translation_discriminant("novel", "published") is Discriminant.TEXT_VERSION
    assert translation_discriminant("light_novel", "unknown") is Discriminant.TEXT_VERSION
    with harness() as h:
        work = h.work("manga")
        translator = h.catalog.create_contributor("Team lead")
        edition = h.catalog.edition_for_translation(
            EditionSpec(work_id=work, lang="en", translation_type="fan", media="images"),
            kind="manga",
            nature="community",
            text_version=TextVersionRef(contributor_id=translator),
        )
        assert edition.text_version == ""
        assert h.rows("SELECT text_version, text_version_contributor_id FROM edition") == [
            {"text_version": "", "text_version_contributor_id": None}
        ]
        binding = h.binding(edition)
        got = h.catalog.ingest(unit(binding, "a", "1", team_label="Alpha"))
        assert got.team_id is not None


@pytest.mark.req("EF-BIB-049")
def test_text_version_references_contributor_not_string() -> None:
    with harness() as h:
        work = h.work("novel")
        contributor = h.catalog.create_contributor("Jane Doe")
        ref = TextVersionRef(contributor_id=contributor)
        edition = h.edition(work, translation_type="official", media="text", text_version=ref)
        row = h.rows(
            "SELECT text_version, text_version_contributor_id, text_version_norm_by FROM edition"
        )[0]
        assert row["text_version_contributor_id"] == contributor
        assert "Jane" not in str(row["text_version"])  # derived from the reference, never a name
        assert str(row["text_version_norm_by"]).startswith("text_version@")
        assert edition.text_version == row["text_version"]
        with pytest.raises(ValueError, match="controlled"):
            TextVersionRef(label="Jane Doe's own cut")
        with pytest.raises(ValueError, match="exactly one"):
            TextVersionRef(contributor_id=contributor, label="abridged")
        labelled = h.edition(
            work,
            translation_type="official",
            media="text",
            text_version=TextVersionRef(label="abridged"),
        )
        assert labelled.id != edition.id


@pytest.mark.req("EF-BIB-049")
def test_community_translation_relay_single_edition() -> None:
    with harness() as h:
        edition = h.edition(kind="light_novel", media="text")
        binding = h.binding(edition)
        for n in range(1, 81):
            team = "Team A" if n <= 40 else "Team B"
            h.catalog.ingest(unit(binding, f"c{n}", str(n), n, team_label=team))
        assert len(h.rows("SELECT id FROM edition")) == 1
        assert len(h.rows("SELECT id FROM team")) == 2
        assert h.catalog.missing_numbers(edition.id) == []  # backfill --gaps sees nothing


@pytest.mark.req("EF-BIB-050")
def test_absent_field_is_not_a_revision() -> None:
    with harness() as h:
        binding = h.binding(h.edition())
        first = h.catalog.ingest(
            unit(binding, "r", "1", 0, revision_hint="v1", content_hash="sha256:" + "a" * 64)
        )
        # the source stopped sending the hash and the hint: nothing changed
        bare = h.catalog.ingest(unit(binding, "r", "1", 1))
        assert bare.outcome is IngestOutcome.UNCHANGED and bare.revision == 1
        changed = h.catalog.ingest(unit(binding, "r", "1", 2, revision_hint="v2"))
        assert changed.outcome is IngestOutcome.REVISION and changed.variant_id == first.variant_id
        # the new row kept the hash the source did not repeat, so a repeat is still unchanged
        again = h.catalog.ingest(
            unit(binding, "r", "1", 3, revision_hint="v2", content_hash="sha256:" + "a" * 64)
        )
        assert again.outcome is IngestOutcome.UNCHANGED


@pytest.mark.req("EF-BIB-050")
def test_reappearing_variant_conflicts_with_its_replacement() -> None:
    with harness() as h:
        binding = h.binding(h.edition())
        a = h.catalog.ingest(unit(binding, "a", "7", 0))
        h.catalog.mark_removed(a.variant_id, at(5))
        b = h.catalog.ingest(unit(binding, "b", "7", 10))
        assert b.outcome is IngestOutcome.SUPERSEDING
        back = h.catalog.ingest(unit(binding, "a", "7", 20))
        assert back.variant_id == a.variant_id and back.suggestion_id is not None
        pair = h.rows("SELECT a, b, reason, state FROM merge_suggestion")
        assert [{pair[0]["a"], pair[0]["b"]}, pair[0]["reason"], pair[0]["state"]] == [
            {a.variant_id, b.variant_id}, "numbering_conflict", "open",
        ]  # fmt: skip
        assert h.scalar("SELECT removed_at IS NULL FROM unit_variant WHERE id = :i", i=a.variant_id)


@pytest.mark.req("EF-BIB-050", "EF-BIB-028")
def test_variant_in_open_conflict_is_never_preferred() -> None:
    with harness() as h:
        binding = h.binding(h.edition())
        old = h.catalog.ingest(unit(binding, "old", "1", 0))
        new = h.catalog.ingest(unit(binding, "new", "1", 5))
        assert new.outcome is IngestOutcome.NUMBERING_CONFLICT
        assert h.catalog.elect_preferred(new.unit_id) is None  # both are named by the suggestion
        h.connection.execute(sa.text("UPDATE merge_suggestion SET state = 'rejected'"))
        assert h.catalog.elect_preferred(new.unit_id) == old.variant_id  # decided: the oldest


@pytest.mark.req("EF-BIB-034")
def test_unnumbered_release_with_known_title_is_only_suggested() -> None:
    with harness() as h:
        binding = h.binding(h.edition())
        numbered = h.catalog.ingest(unit(binding, "n", "12", 0, title="The  Long Night"))
        extra = h.catalog.ingest(
            unit(binding, "x", "Extra", 1, source_order=1, title="the long night")
        )
        assert extra.unit_id != numbered.unit_id  # never attached on its own
        assert extra.suggestion_id is not None
        row = h.rows("SELECT a, b, reason, state FROM merge_suggestion")[0]
        assert {row["a"], row["b"]} == {numbered.variant_id, extra.variant_id}
        assert (row["reason"], row["state"]) == ("unnumbered_title_match", "open")
        other = h.catalog.ingest(unit(binding, "y", "Extra", 2, source_order=2, title="Other"))
        assert other.suggestion_id is None
        # a title match does not block the export of either Variant
        assert h.catalog.elect_preferred(extra.unit_id) == extra.variant_id
