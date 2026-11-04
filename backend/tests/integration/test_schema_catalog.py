"""M1a schema, catalog and acquisition (migration 0003): constraints that bite."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine, exc

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, identity, metadata

SHA = "sha256:" + "a" * 64
ARGON = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(tmp_path / "catalog.db")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


def rejected(connection: sa.Connection, statement: Any, expected: str) -> None:
    """The statement fails, and for the reason named: a constraint, or a message fragment."""
    with pytest.raises((exc.IntegrityError, exc.StatementError, exc.DatabaseError)) as raised:
        connection.execute(statement)
    assert expected in str(raised.value.orig if raised.value.orig else raised.value), raised.value


def add_kind(connection: sa.Connection, key: str = "manga") -> str:
    connection.execute(
        sa.insert(catalog.work_kind)
        .values(key=key, label_fr=key, default_media="images")
        .prefix_with("OR IGNORE")
    )
    return key


def add_work(connection: sa.Connection, **extra: Any) -> str:
    work_id = new_ulid()
    values: dict[str, Any] = {"id": work_id, "kind": add_kind(connection), "title": "Un titre"}
    values["sort_title"] = "un titre"
    connection.execute(sa.insert(catalog.work).values(**{**values, **extra}))
    return work_id


def edition_values(work_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": new_ulid(),
        "work_id": work_id,
        "lang": "fr",
        "translation_type": "official",
        "media": "images",
        **extra,
    }


def add_edition(connection: sa.Connection, work_id: str, **extra: Any) -> str:
    values = edition_values(work_id, **extra)
    connection.execute(sa.insert(catalog.edition).values(**values))
    return str(values["id"])


def add_source_item(connection: sa.Connection, remote_id: str = "series-1") -> str:
    item_id = new_ulid()
    connection.execute(
        sa.insert(acquisition.source_item).values(
            id=item_id, plugin_id="suwayomi", source_key="suwayomi:1", remote_id=remote_id
        )
    )
    return item_id


def binding_values(work_id: str, edition_id: str, item_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": new_ulid(),
        "work_id": work_id,
        "edition_id": edition_id,
        "source_item_id": item_id,
        "priority": 1,
        "active": True,
        **extra,
    }


def add_binding(
    connection: sa.Connection, work_id: str, edition_id: str, item_id: str, **extra: Any
) -> str:
    values = binding_values(work_id, edition_id, item_id, **extra)
    connection.execute(sa.insert(acquisition.source_binding).values(**values))
    return str(values["id"])


def add_unit(connection: sa.Connection, edition_id: str, number: str = "1", **extra: Any) -> str:
    unit_id = new_ulid()
    values: dict[str, Any] = {
        "id": unit_id,
        "edition_id": edition_id,
        "number_raw": number,
        "number_norm": number,
        "number_sort": float(number) if number else None,
        "track": "main",
        "kind": "chapter",
        **extra,
    }
    connection.execute(sa.insert(catalog.unit).values(**values))
    return unit_id


def add_variant(
    connection: sa.Connection, unit_id: str, binding_id: str, remote_unit_id: str = "ch-1"
) -> str:
    variant_id = new_ulid()
    connection.execute(
        sa.insert(catalog.unit_variant).values(
            id=variant_id,
            unit_id=unit_id,
            source_binding_id=binding_id,
            remote_unit_id=remote_unit_id,
            lang="fr",
        )
    )
    return variant_id


@pytest.fixture
def chain(engine: Engine) -> dict[str, str]:
    """work > edition > binding > unit > variant, the spine of the model."""
    with engine.begin() as connection:
        work_id = add_work(connection)
        edition_id = add_edition(connection, work_id)
        item_id = add_source_item(connection)
        binding_id = add_binding(connection, work_id, edition_id, item_id)
        unit_id = add_unit(connection, edition_id)
        variant_id = add_variant(connection, unit_id, binding_id)
    return {
        "work": work_id,
        "edition": edition_id,
        "item": item_id,
        "binding": binding_id,
        "unit": unit_id,
        "variant": variant_id,
    }


# ---------------------------------------------------------------- identifiers

API_ID_TABLES = [
    (catalog.series, "id"),
    (catalog.work, "id"),
    (catalog.contributor, "id"),
    (catalog.edition, "id"),
    (catalog.volume, "id"),
    (catalog.unit, "id"),
    (catalog.unit_variant, "id"),
    (catalog.team, "id"),
    (catalog.merge_suggestion, "id"),
    (catalog.age_suggestion, "id"),
    (acquisition.source_item, "id"),
    (acquisition.source_binding, "id"),
    (acquisition.download_request, "id"),
    (acquisition.source_account, "id"),
    (acquisition.upstream_snapshot, "id"),
    (acquisition.revision_pending, "id"),
]


@pytest.mark.req("ENF-OPS-020")
@pytest.mark.parametrize(("table", "column"), API_ID_TABLES, ids=lambda v: getattr(v, "name", v))
def test_api_ids_are_opaque_ulids(table: sa.Table, column: str) -> None:
    """Catalog and acquisition volet of `test_api_ids_are_opaque_ulids`."""
    assert isinstance(table.c[column].type, sa.String)
    assert table.c[column].type.length == 26
    assert table.c[column].primary_key
    sql = {str(c.sqltext) for c in table.constraints if isinstance(c, sa.CheckConstraint)}
    assert any(column in text and "GLOB" in text for text in sql), sql


@pytest.mark.req("ENF-OPS-020")
def test_a_non_ulid_work_identifier_is_refused(engine: Engine) -> None:
    with engine.begin() as connection:
        add_kind(connection)
        for bad in ("1", "not-a-ulid", new_ulid().lower(), new_ulid() + "0"):
            rejected(
                connection,
                sa.insert(catalog.work).values(id=bad, kind="manga", title="t", sort_title="t"),
                "ck_work_id_ulid",
            )


# ------------------------------------------------------------------- edition


@pytest.mark.req("EF-BIB-001")
def test_edition_unique_key_no_null(engine: Engine) -> None:
    with engine.begin() as connection:
        work_id = add_work(connection)
        add_edition(connection, work_id, publisher="Glénat")
        rejected(
            connection,
            sa.insert(catalog.edition).values(**edition_values(work_id, publisher="Glénat")),
            "UNIQUE constraint failed",
        )
        # a NULL would never collide: every part of the key is NOT NULL, with '' as sentinel
        for column in ("publisher", "text_version", "performance", "packaging"):
            rejected(
                connection,
                sa.insert(catalog.edition).values(**edition_values(work_id, **{column: None})),
                "NOT NULL constraint failed",
            )
        row = connection.execute(
            sa.select(
                catalog.edition.c.publisher,
                catalog.edition.c.text_version,
                catalog.edition.c.performance,
                catalog.edition.c.packaging,
            ).where(catalog.edition.c.work_id == work_id)
        ).one()
        assert row == ("Glénat", "", "", "")
        add_edition(connection, work_id)  # publisher '' (unknown) is its own value
        rejected(
            connection,
            sa.insert(catalog.edition).values(**edition_values(work_id)),
            "UNIQUE constraint failed",
        )
        add_edition(connection, work_id, lang="en")
        add_edition(connection, work_id, translation_type="fan")
        add_edition(connection, add_work(connection))  # same key, another work


@pytest.mark.req("EF-BIB-001")
def test_edition_key_performance_sentinel(engine: Engine) -> None:
    with engine.begin() as connection:
        work_id = add_work(connection)
        add_edition(connection, work_id, media="audio")
        add_edition(connection, work_id, media="audio", performance="nrt:contributor-a")
        add_edition(connection, work_id, media="audio", performance="nrt:contributor-b")
        rejected(
            connection,
            sa.insert(catalog.edition).values(
                **edition_values(work_id, media="audio", performance="nrt:contributor-a")
            ),
            "UNIQUE constraint failed",
        )
        rejected(
            connection,
            sa.insert(catalog.edition).values(**edition_values(work_id, media="audio")),
            "UNIQUE constraint failed",
        )


@pytest.mark.req("EF-BIB-001")
def test_two_translations_of_one_novel_are_two_editions(engine: Engine) -> None:
    with engine.begin() as connection:
        translator = new_ulid()
        connection.execute(
            sa.insert(catalog.contributor).values(
                id=translator, kind="person", primary_name="A. Traductrice"
            )
        )
        work_id = add_work(connection, kind=add_kind(connection, "novel"))
        add_edition(
            connection,
            work_id,
            media="text",
            text_version="abridged",
            text_version_label="abridged",
        )
        add_edition(
            connection,
            work_id,
            media="text",
            text_version="a-traductrice",
            text_version_contributor_id=translator,
        )
        # the text version is derived from a reference, never from a bare name string
        rejected(
            connection,
            sa.insert(catalog.edition).values(
                **edition_values(work_id, media="text", text_version="made-up")
            ),
            "ck_edition_text_version_has_reference",
        )


@pytest.mark.req("ENF-I18N-005")
def test_edition_language_is_a_real_tag_never_a_wildcard(engine: Engine) -> None:
    with engine.begin() as connection:
        work_id = add_work(connection)
        for bad in ("all", "multi", "und", "mul", "", "fr_FR", "fr FR"):
            rejected(
                connection,
                sa.insert(catalog.edition).values(**edition_values(work_id, lang=bad)),
                "ck_edition_lang",
            )
        add_edition(connection, work_id, lang="pt-BR", lang_raw="pt_br")


@pytest.mark.req("EF-BIB-001")
def test_edition_vocabularies_are_checked(engine: Engine) -> None:
    with engine.begin() as connection:
        work_id = add_work(connection)
        rejected(
            connection,
            sa.insert(catalog.edition).values(**edition_values(work_id, translation_type="x")),
            "ck_edition_translation_type_values",
        )
        rejected(
            connection,
            sa.insert(catalog.edition).values(**edition_values(work_id, media="video")),
            "ck_edition_media_values",
        )
        rejected(
            connection,
            sa.insert(catalog.edition).values(**edition_values(work_id, volume_policy="x")),
            "ck_edition_volume_policy_values",
        )
        row = connection.execute(
            sa.select(catalog.edition.c.volume_policy).where(
                catalog.edition.c.id == add_edition(connection, work_id, lang="de")
            )
        ).scalar_one()
        assert row == "closed_only"


@pytest.mark.req("EF-BIB-001")
def test_series_is_optional_above_work(engine: Engine) -> None:
    with engine.begin() as connection:
        work_id = add_work(connection)  # no series: a work stands alone
        series_id = new_ulid()
        connection.execute(
            sa.insert(catalog.series).values(
                id=series_id, title="Cycle", sort_title="cycle", reading_order="publication"
            )
        )
        connection.execute(
            sa.insert(catalog.series_member).values(
                series_id=series_id, work_id=work_id, position="1.5", label="Tome 1.5"
            )
        )
        rejected(
            connection,
            sa.insert(catalog.series).values(
                id=new_ulid(), title="x", sort_title="x", reading_order="random"
            ),
            "ck_series_reading_order_values",
        )
        rejected(
            connection,
            sa.insert(catalog.series_member).values(
                series_id=series_id, work_id=add_work(connection), position="one"
            ),
            "ck_series_member_position_decimal",
        )


# ---------------------------------------------------------------------- age


@pytest.mark.req("EF-EXP-011")
def test_age_stored_as_min_age_int_and_x_flag(engine: Engine) -> None:
    with engine.begin() as connection:
        work_id = add_work(connection, age_rating=16, age_x=False, rated_at=NOW)
        unrated = add_work(connection)
        row = connection.execute(
            sa.select(
                catalog.work.c.age_rating, catalog.work.c.age_x, catalog.work.c.rated_at
            ).where(catalog.work.c.id.in_([unrated]))
        ).one()
        assert row == (None, False, None)  # not rated is NULL, never 0 ("all audiences")
        stored = connection.execute(
            sa.select(sa.func.typeof(catalog.work.c.age_rating), catalog.work.c.age_rating).where(
                catalog.work.c.id == work_id
            )
        ).one()
        assert stored == ("integer", 16)
        add_work(connection, age_rating=0)
        add_work(connection, age_rating=18, age_x=True)
        add_work(connection, age_rating=21)  # a new tier needs no migration
        for bad in ("16+", "sixteen", -1, 1000, 12.5):
            rejected(
                connection,
                sa.insert(catalog.work).values(
                    id=new_ulid(), kind="manga", title="t", sort_title="t", age_rating=bad
                ),
                "ck_work_age_rating_min_age",
            )
        # same pair of columns on series and edition (02 section 3.14)
        for table in (catalog.series, catalog.edition):
            assert {"age_rating", "age_x", "rated_at"} <= set(table.c.keys())
            assert isinstance(table.c.age_x.type, sa.Boolean)
        work = add_work(connection)
        rejected(
            connection,
            sa.insert(catalog.edition).values(**edition_values(work, age_rating="18+")),
            "ck_edition_age_rating_min_age",
        )


@pytest.mark.req("EF-EXP-011")
def test_age_suggestion_is_never_a_rating(engine: Engine) -> None:
    with engine.begin() as connection:
        work_id = add_work(connection)
        connection.execute(
            sa.insert(catalog.age_suggestion).values(
                id=new_ulid(),
                entity_type="work",
                entity_id=work_id,
                value=18,
                x=False,
                provenance="comicinfo",
                seen_at=NOW,
            )
        )
        state = connection.execute(sa.select(catalog.age_suggestion.c.state)).scalar_one()
        assert state == "open"
        rejected(
            connection,
            sa.insert(catalog.age_suggestion).values(
                id=new_ulid(),
                entity_type="series",
                entity_id=work_id,
                value=18,
                provenance="comicinfo",
                seen_at=NOW,
            ),
            "ck_age_suggestion_entity_type_values",
        )


# -------------------------------------------------------------------- dates


@pytest.mark.req("EF-BIB-047")
def test_partial_dates_keep_the_precision_they_were_given(engine: Engine) -> None:
    with engine.begin() as connection:
        add_work(connection, first_published="1998", date_precision="year")
        add_work(connection, first_published="1998-07", date_precision="month")
        add_work(connection, first_published="1998-07-31", date_precision="day")
        add_work(connection)  # no date, no precision
        for date, precision in [
            ("1998", "day"),  # a precision that was never there
            ("1998-07", "year"),
            ("1998-07-31", "month"),
            ("1998-02-30", "day"),  # not a calendar day
            ("1998-13", "month"),
            ("98", "year"),
            ("1998", None),
            (None, "year"),
        ]:
            rejected(
                connection,
                sa.insert(catalog.work).values(
                    id=new_ulid(),
                    kind="manga",
                    title="t",
                    sort_title="t",
                    first_published=date,
                    date_precision=precision,
                ),
                "ck_work_date_precision_coherent",
            )


# ---------------------------------------------------------- units and variants


@pytest.mark.req("EF-BIB-034", "EF-BIB-050")
def test_unit_place_is_unique_within_an_edition(engine: Engine, chain: dict[str, str]) -> None:
    with engine.begin() as connection:
        edition = chain["edition"]
        rejected(
            connection,
            sa.insert(catalog.unit).values(
                id=new_ulid(),
                edition_id=edition,
                number_raw="01",
                number_norm="1",
                track="main",
                kind="chapter",
            ),
            "UNIQUE constraint failed",
        )
        add_unit(connection, edition, "1", part="b")  # a part is another place
        add_unit(connection, edition, "1", track="special")  # so is another track
        add_unit(connection, edition, "1", number_segment="S2", number_segment_ordinal=2)
        add_unit(connection, edition, "1", number_segment="S10", number_segment_ordinal=10)
        add_unit(connection, add_edition(connection, chain["work"], lang="en"), "1")


@pytest.mark.req("EF-BIB-034")
def test_unnumbered_extras_ordered_by_source_order(engine: Engine, chain: dict[str, str]) -> None:
    with engine.begin() as connection:
        edition = chain["edition"]
        for order in (1, 2, 3):
            add_unit(
                connection, edition, "", track="unnumbered", source_order=order, number_sort=None
            )
        rejected(  # the place of an extra is its source order
            connection,
            sa.insert(catalog.unit).values(
                id=new_ulid(),
                edition_id=edition,
                number_raw="",
                number_norm="",
                track="unnumbered",
                source_order=2,
                kind="chapter",
            ),
            "UNIQUE constraint failed",
        )
        rejected(  # a numbered track needs a number
            connection,
            sa.insert(catalog.unit).values(
                id=new_ulid(),
                edition_id=edition,
                number_raw="",
                number_norm="",
                track="main",
                kind="chapter",
            ),
            "ck_unit_numbered_track_has_number",
        )


@pytest.mark.req("EF-BIB-050")
def test_unit_numbering_provenance_and_vocabularies(engine: Engine, chain: dict[str, str]) -> None:
    with engine.begin() as connection:
        add_unit(connection, chain["edition"], "2", number_norm_by="chapter-number@1")
        for column, bad, constraint in [
            ("number_norm_by", "chapter-number", "ck_unit_number_norm_by_versioned"),
            ("track", "bonus", "ck_unit_track_values"),
            ("state", "gone", "ck_unit_state_values"),
            ("availability", "maybe", "ck_unit_availability_values"),
            ("kind", "page", "ck_unit_kind_values"),
        ]:
            rejected(
                connection,
                sa.insert(catalog.unit).values(
                    **{
                        "id": new_ulid(),
                        "edition_id": chain["edition"],
                        "number_raw": "9",
                        "number_norm": "9",
                        "track": "main",
                        "kind": "chapter",
                        column: bad,
                    }
                ),
                constraint,
            )
        row = connection.execute(
            sa.select(
                catalog.unit.c.number_segment,
                catalog.unit.c.part,
                catalog.unit.c.state,
                catalog.unit.c.follow_suppressed,
            ).where(catalog.unit.c.id == chain["unit"])
        ).one()
        assert row == ("", "", "known", False)


@pytest.mark.req("EF-EXP-018")
def test_follow_suppressed_is_persistent_and_attributed(
    engine: Engine, chain: dict[str, str]
) -> None:
    assert {"follow_suppressed", "follow_suppressed_by", "follow_suppressed_at"} <= set(
        catalog.unit.c.keys()
    )
    with engine.begin() as connection:
        user_id = new_ulid()
        connection.execute(
            sa.insert(identity.user).values(
                id=user_id, username="a", password_hash=ARGON, role="admin", can_rate=True
            )
        )
        connection.execute(
            sa.update(catalog.unit)
            .where(catalog.unit.c.id == chain["unit"])
            .values(follow_suppressed=True, follow_suppressed_by=user_id, follow_suppressed_at=NOW)
        )


@pytest.mark.req("EF-BIB-005", "EF-BIB-050")
def test_variant_identity_is_binding_and_remote_unit_id(
    engine: Engine, chain: dict[str, str]
) -> None:
    with engine.begin() as connection:
        rejected(
            connection,
            sa.insert(catalog.unit_variant).values(
                id=new_ulid(),
                unit_id=chain["unit"],
                source_binding_id=chain["binding"],
                remote_unit_id="ch-1",
                lang="fr",
            ),
            "UNIQUE constraint failed",
        )
        # a new remote id is a new variant, even for the same place
        second = add_variant(connection, chain["unit"], chain["binding"], "ch-1-bis")
        connection.execute(
            sa.update(catalog.unit_variant)
            .where(catalog.unit_variant.c.id == second)
            .values(supersedes_variant_id=chain["variant"], removed_at=None)
        )
        revision = connection.execute(
            sa.select(catalog.unit_variant.c.revision).where(
                catalog.unit_variant.c.id == chain["variant"]
            )
        ).scalar_one()
        assert revision == 1
        # the same remote id on another binding is another variant
        other = add_binding(
            connection,
            chain["work"],
            chain["edition"],
            add_source_item(connection, "series-2"),
            priority=2,
        )
        add_variant(connection, chain["unit"], other, "ch-1")


@pytest.mark.req("EF-BIB-050")
def test_variant_revision_append_only(engine: Engine, chain: dict[str, str]) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.insert(catalog.variant_revision).values(
                variant_id=chain["variant"],
                revision=1,
                seen_at=NOW,
                page_count=20,
                content_hash=SHA,
            )
        )
        connection.execute(
            sa.insert(catalog.variant_revision).values(
                variant_id=chain["variant"],
                revision=2,
                seen_at=NOW,
                page_count=1,
                revision_hint="stub",
            )
        )
        rejected(
            connection,
            sa.insert(catalog.variant_revision).values(
                variant_id=chain["variant"], revision=2, seen_at=NOW
            ),
            "append-only",
        )
    for statement in (
        sa.update(catalog.variant_revision).values(page_count=99),
        sa.delete(catalog.variant_revision),
    ):
        with engine.begin() as connection:
            rejected(connection, statement, "append-only")
    with engine.begin() as connection:
        remaining = connection.execute(
            sa.select(sa.func.count()).select_from(catalog.variant_revision)
        ).scalar_one()
        assert remaining == 2


@pytest.mark.req("EF-BIB-052")
def test_variant_revisions_leave_with_a_purged_variant(
    engine: Engine, chain: dict[str, str]
) -> None:
    """Append-only protects the history of a live variant; an erasure removes the whole variant."""
    with engine.begin() as connection:
        connection.execute(
            sa.insert(catalog.variant_revision).values(
                variant_id=chain["variant"], revision=1, seen_at=NOW
            )
        )
        connection.execute(
            sa.delete(catalog.unit_variant).where(catalog.unit_variant.c.id == chain["variant"])
        )
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(catalog.variant_revision)
            ).scalar_one()
            == 0
        )


@pytest.mark.req("EF-BIB-035")
def test_team_belongs_to_the_edition_not_the_binding(engine: Engine, chain: dict[str, str]) -> None:
    assert "source_binding_id" not in catalog.team.c
    with engine.begin() as connection:
        team = new_ulid()
        connection.execute(
            sa.insert(catalog.team).values(
                id=team,
                edition_id=chain["edition"],
                label="team-a",
                label_raw="Team A",
                label_norm_by="team-label@1",
            )
        )
        rejected(
            connection,
            sa.insert(catalog.team).values(
                id=new_ulid(), edition_id=chain["edition"], label="team-a", label_raw="TEAM A"
            ),
            "UNIQUE constraint failed",
        )
        connection.execute(
            sa.insert(catalog.team_alias).values(
                team_id=team, source_binding_id=chain["binding"], label="Team A", first_seen_at=NOW
            )
        )
        connection.execute(
            sa.insert(catalog.team_priority).values(
                edition_id=chain["edition"], team_id=team, rank=1
            )
        )
        other = new_ulid()
        connection.execute(
            sa.insert(catalog.team).values(id=other, edition_id=chain["edition"], label="team-b")
        )
        rejected(
            connection,
            sa.insert(catalog.team_priority).values(
                edition_id=chain["edition"], team_id=other, rank=1
            ),
            "UNIQUE constraint failed",
        )


# ------------------------------------------------------------ source bindings


@pytest.mark.req("EF-EXP-053", "EF-SRC-037")
def test_binding_priority_unique_among_active_bindings_of_edition(
    engine: Engine, chain: dict[str, str]
) -> None:
    with engine.begin() as connection:
        item_two = add_source_item(connection, "series-2")
        rejected(  # same edition, same rank, both active
            connection,
            sa.insert(acquisition.source_binding).values(
                **binding_values(chain["work"], chain["edition"], item_two, priority=1)
            ),
            "UNIQUE constraint failed",
        )
        second = add_binding(connection, chain["work"], chain["edition"], item_two, priority=2)
        # a disabled binding keeps its rank without blocking anyone
        third = add_binding(
            connection,
            chain["work"],
            chain["edition"],
            add_source_item(connection, "series-3"),
            priority=1,
            active=False,
            disabled_reason="manual",
        )
        # another edition has its own ranking
        other_edition = add_edition(connection, chain["work"], lang="en")
        add_binding(
            connection,
            chain["work"],
            other_edition,
            add_source_item(connection, "series-4"),
            priority=1,
        )
        # re-activating the disabled one collides with the active rank 1
        rejected(
            connection,
            sa.update(acquisition.source_binding)
            .where(acquisition.source_binding.c.id == third)
            .values(active=True, disabled_reason=None),
            "UNIQUE constraint failed",
        )
        # freeing a rank by disabling its holder lets another binding take it
        connection.execute(
            sa.update(acquisition.source_binding)
            .where(acquisition.source_binding.c.id == chain["binding"])
            .values(active=False, disabled_reason="removed_upstream")
        )
        connection.execute(
            sa.update(acquisition.source_binding)
            .where(acquisition.source_binding.c.id == second)
            .values(priority=1)
        )


@pytest.mark.req("EF-SRC-037")
def test_binding_belongs_to_one_edition_of_its_work(engine: Engine, chain: dict[str, str]) -> None:
    assert acquisition.source_binding.c.edition_id.nullable is False
    with engine.begin() as connection:
        rejected(
            connection,
            sa.insert(acquisition.source_binding).values(
                **{
                    **binding_values(
                        chain["work"], chain["edition"], add_source_item(connection, "s-2")
                    ),
                    "edition_id": None,
                }
            ),
            "NOT NULL constraint failed",
        )
        stranger = add_work(connection)  # an edition of another work
        rejected(
            connection,
            sa.insert(acquisition.source_binding).values(
                **binding_values(
                    stranger, chain["edition"], add_source_item(connection, "s-3"), priority=3
                )
            ),
            "FOREIGN KEY constraint failed",
        )
        # the same upstream series cannot be bound twice to one edition
        rejected(
            connection,
            sa.insert(acquisition.source_binding).values(
                **binding_values(chain["work"], chain["edition"], chain["item"], priority=9)
            ),
            "UNIQUE constraint failed",
        )


@pytest.mark.req("EF-SRC-037")
def test_binding_disabled_reason_goes_with_inactivity(
    engine: Engine, chain: dict[str, str]
) -> None:
    with engine.begin() as connection:
        item = add_source_item(connection, "s-2")
        rejected(
            connection,
            sa.insert(acquisition.source_binding).values(
                **binding_values(chain["work"], chain["edition"], item, priority=5, active=False)
            ),
            "ck_source_binding_disabled_reason_matches_active",
        )
        rejected(
            connection,
            sa.insert(acquisition.source_binding).values(
                **binding_values(
                    chain["work"], chain["edition"], item, priority=5, disabled_reason="manual"
                )
            ),
            "ck_source_binding_disabled_reason_matches_active",
        )
        for reason in ("not_loaded", "content_failures", "removed_upstream", "manual"):
            add_binding(
                connection,
                chain["work"],
                chain["edition"],
                add_source_item(connection, reason),
                priority=7,
                active=False,
                disabled_reason=reason,
            )
        rejected(
            connection,
            sa.insert(acquisition.source_binding).values(
                **binding_values(
                    chain["work"],
                    chain["edition"],
                    item,
                    priority=5,
                    active=False,
                    disabled_reason="bored",
                )
            ),
            "ck_source_binding_disabled_reason_values",
        )
        trust = connection.execute(
            sa.select(acquisition.source_binding.c.content_trust).where(
                acquisition.source_binding.c.id == chain["binding"]
            )
        ).scalar_one()
        assert trust == "untrusted"  # the safe default (EF-BIB-051)


@pytest.mark.req("EF-EXP-053", "EF-SRC-037")
def test_series_follow_keyed_by_edition_not_binding(engine: Engine, chain: dict[str, str]) -> None:
    follow = acquisition.series_follow
    assert [c.name for c in follow.primary_key.columns] == ["edition_id"]
    assert "source_binding_id" not in follow.c
    with engine.begin() as connection:
        connection.execute(
            sa.insert(follow).values(
                edition_id=chain["edition"],
                work_id=chain["work"],
                follow_from_segment="",
                follow_from_number="12",
                follow_from_norm_by="chapter-number@1",
            )
        )
        row = connection.execute(
            sa.select(
                follow.c.backfill_mode, follow.c.upgrade_policy, follow.c.filters, follow.c.cap
            )
        ).one()
        assert row == ("none", "keep_first", None, None)  # filters NULL: inherit
        rejected(  # one follow per edition, whichever bindings it has
            connection,
            sa.insert(follow).values(edition_id=chain["edition"], work_id=chain["work"]),
            "UNIQUE constraint failed",
        )
        stranger = add_work(connection)
        rejected(
            connection,
            sa.insert(follow).values(
                edition_id=add_edition(connection, chain["work"], lang="en"), work_id=stranger
            ),
            "FOREIGN KEY constraint failed",
        )
        for column, bad, constraint in [
            ("backfill_mode", "everything", "ck_series_follow_backfill_mode_values"),
            ("upgrade_policy", "always", "ck_series_follow_upgrade_policy_values"),
            ("check_override", "sleeping", "ck_series_follow_check_override_values"),
            ("cap", 0, "ck_series_follow_cap_min"),
        ]:
            edition = add_edition(connection, chain["work"], lang=f"x-{column[:3]}")
            rejected(
                connection,
                sa.insert(follow).values(
                    edition_id=edition, work_id=chain["work"], **{column: bad}
                ),
                constraint,
            )


@pytest.mark.req("EF-BIB-056")
def test_gap_ack_is_per_place(engine: Engine, chain: dict[str, str]) -> None:
    with engine.begin() as connection:
        key = {
            "edition_id": chain["edition"],
            "track": "main",
            "number_segment": "",
            "number_norm": "7",
        }
        connection.execute(sa.insert(acquisition.gap_ack).values(**key, acked_at=NOW))
        rejected(
            connection,
            sa.insert(acquisition.gap_ack).values(**key, acked_at=NOW),
            "UNIQUE constraint failed",
        )


# ----------------------------------------------------------- download tickets


@pytest.mark.req("EF-SRC-034", "EF-SRC-033")
def test_download_request_targets_a_unit_or_a_range(engine: Engine, chain: dict[str, str]) -> None:
    table = acquisition.download_request
    base = {"source_binding_id": chain["binding"], "ticket": "opaque-1"}
    with engine.begin() as connection:
        request = new_ulid()
        connection.execute(
            sa.insert(table).values(
                id=request, target_kind="unit", variant_id=chain["variant"], **base
            )
        )
        connection.execute(
            sa.insert(table).values(
                id=new_ulid(),
                target_kind="range",
                range_ref={"edition_id": chain["edition"]},
                source_binding_id=chain["binding"],
            )
        )
        defaults = connection.execute(
            sa.select(table.c.state, table.c.attempts).where(table.c.id == request)
        ).one()
        assert defaults == ("requested", 0)
        for values in (
            {"target_kind": "unit"},  # a unit request names its variant
            {"target_kind": "range", "variant_id": chain["variant"]},
            {"target_kind": "unit", "variant_id": chain["variant"], "range_ref": {}},
            {"target_kind": "page", "variant_id": chain["variant"]},
        ):
            rejected(connection, sa.insert(table).values(id=new_ulid(), **base, **values), "ck_")
        rejected(  # one live ticket per binding
            connection,
            sa.insert(table).values(
                id=new_ulid(), target_kind="unit", variant_id=chain["variant"], **base
            ),
            "UNIQUE constraint failed",
        )
        connection.execute(
            sa.insert(acquisition.download_request_unit).values(
                download_request_id=request, unit_id=chain["unit"]
            )
        )
        rejected(
            connection,
            sa.insert(acquisition.download_request_unit).values(
                download_request_id=request, unit_id=chain["unit"]
            ),
            "UNIQUE constraint failed",
        )


@pytest.mark.req("EF-BIB-052")
def test_download_request_can_be_closed_by_purge(engine: Engine, chain: dict[str, str]) -> None:
    table = acquisition.download_request
    with engine.begin() as connection:
        request = new_ulid()
        connection.execute(
            sa.insert(table).values(
                id=request,
                target_kind="unit",
                variant_id=chain["variant"],
                source_binding_id=chain["binding"],
                ticket="t",
            )
        )
        for state in ("downloaded", "fetched", "cancelled", "abandoned", "closed_by_purge"):
            connection.execute(sa.update(table).where(table.c.id == request).values(state=state))
        rejected(
            connection,
            sa.update(table).where(table.c.id == request).values(state="lost"),
            "ck_download_request_state_values",
        )


# ------------------------------------------------------------------- sources


@pytest.mark.req("EF-SRC-008", "EF-SRC-036")
def test_source_item_identity_and_alias(engine: Engine) -> None:
    with engine.begin() as connection:
        add_source_item(connection, "abc")
        rejected(
            connection,
            sa.insert(acquisition.source_item).values(
                id=new_ulid(), plugin_id="suwayomi", source_key="suwayomi:1", remote_id="abc"
            ),
            "UNIQUE constraint failed",
        )
        connection.execute(
            sa.insert(acquisition.source_item).values(
                id=new_ulid(), plugin_id="suwayomi", source_key="suwayomi:2", remote_id="abc"
            )
        )
        connection.execute(
            sa.insert(acquisition.source_alias).values(
                plugin_id="suwayomi",
                old_source_key="suwayomi:1",
                old_remote_id="abc",
                new_source_key="suwayomi:9",
                new_remote_id="xyz",
                id_scheme="v2",
            )
        )
        rejected(
            connection,
            sa.insert(acquisition.source_alias).values(
                plugin_id="suwayomi",
                old_source_key="suwayomi:1",
                old_remote_id="abc",
                new_source_key="suwayomi:8",
                new_remote_id="uvw",
            ),
            "UNIQUE constraint failed",
        )


@pytest.mark.req("ENF-LEG-001")
def test_no_sub_source_is_active_at_installation(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.insert(acquisition.sub_source).values(
                plugin_id="suwayomi", source_key="suwayomi:1", name="Some source", lang="mul"
            )
        )
        enabled = connection.execute(sa.select(acquisition.sub_source.c.enabled)).scalar_one()
        assert enabled is False


@pytest.mark.req("EF-SRC-019")
def test_plugin_install_third_party_disabled_and_hash_prefixed(engine: Engine) -> None:
    table = acquisition.plugin_install
    base = {"plugin_id": "demo", "version": "1.0.0", "api": "1", "origin": "local"}
    with engine.begin() as connection:
        for bad in ("a" * 64, "md5:" + "a" * 32, "sha256:" + "A" * 64):
            rejected(
                connection,
                sa.insert(table).values(**base, sha256=bad),
                "ck_plugin_install_sha256_sha256_prefixed",
            )
        connection.execute(sa.insert(table).values(**base, sha256=SHA))
        assert connection.execute(sa.select(table.c.enabled)).scalar_one() is False
        rejected(
            connection,
            sa.insert(table).values(**{**base, "plugin_id": "other", "origin": "web"}, sha256=SHA),
            "ck_plugin_install_origin_values",
        )
        connection.execute(
            sa.insert(acquisition.plugin_state).values(
                plugin_id="demo", key="cursor", payload={"page": 3}, payload_schema=1
            )
        )


@pytest.mark.req("EF-BIB-042")
def test_source_failure_counts_per_upstream_source(engine: Engine, chain: dict[str, str]) -> None:
    table = acquisition.source_failure
    key = {"source_binding_id": chain["binding"], "limit_key": "suwayomi:1"}
    with engine.begin() as connection:
        connection.execute(
            sa.insert(table).values(**key, failure_class="content", consecutive_content_errors=2)
        )
        connection.execute(sa.insert(table).values(**key, failure_class="auth"))
        rejected(
            connection,
            sa.insert(table).values(**key, failure_class="content"),
            "UNIQUE constraint failed",
        )
        rejected(
            connection,
            sa.insert(table).values(**key, failure_class="weather"),
            "ck_source_failure_failure_class_values",
        )


@pytest.mark.req("ENF-SEC-005", "EF-SRC-023")
def test_source_secrets_are_sealed_or_absent(engine: Engine) -> None:
    config = acquisition.source_config
    with engine.begin() as connection:
        connection.execute(
            sa.insert(config).values(plugin_id="suwayomi", settings={"url": "http://x"})
        )
        row = connection.execute(
            sa.select(config.c.family_safe, config.c.consecutive_failures, config.c.secret_ct)
        ).one()
        assert row == (False, 0, None)
        connection.execute(
            sa.insert(coherence_secret_key()).values(
                kid="k1", state="active", alg="xchacha20poly1305"
            )
        )
        sealed = {
            "secret_kid": "k1",
            "secret_v": 1,
            "secret_nonce": b"n" * 12,  # any algorithm's nonce: the alg is read from secret_key
            "secret_ct": b"c" * 16,
        }
        for broken in (
            {"secret_kid": None},  # ciphertext without key id
            {"secret_v": None},
            {"secret_v": 0},
            {"secret_nonce": None},
            {"secret_ct": None},
            {"secret_ct": b"short"},  # less than an AEAD tag
        ):
            rejected(
                connection,
                sa.update(config)
                .where(config.c.plugin_id == "suwayomi")
                .values(**{**sealed, **broken}),
                "ck_source_config_secret_sealed",
            )
        connection.execute(
            sa.update(config).where(config.c.plugin_id == "suwayomi").values(**sealed)
        )


def coherence_secret_key() -> sa.Table:
    from shelfhold.infrastructure.schema import coherence

    return coherence.secret_key


@pytest.mark.req("EF-SRC-031")
def test_source_account_kind_and_auth_mode(engine: Engine) -> None:
    table = acquisition.source_account
    with engine.begin() as connection:
        user_id = new_ulid()
        connection.execute(
            sa.insert(identity.user).values(
                id=user_id, username="a", password_hash=ARGON, role="admin", can_rate=True
            )
        )
        values = {
            "user_id": user_id,
            "plugin_id": "suwayomi",
            "kind": "service",
            "auth_mode": "basic_auth",
        }
        connection.execute(sa.insert(table).values(id=new_ulid(), **values))
        rejected(
            connection,
            sa.insert(table).values(id=new_ulid(), **values),
            "UNIQUE constraint failed",
        )
        rejected(
            connection,
            sa.insert(table).values(id=new_ulid(), **{**values, "kind": "root"}),
            "ck_source_account_kind_values",
        )


@pytest.mark.req("EF-EXP-056")
def test_upstream_snapshot_is_kept_until_detach(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.insert(acquisition.upstream_snapshot).values(
                id=new_ulid(),
                connected_service_id="suwayomi",
                series_ref="42",
                capability="upstream_restore",
                payload={"category": 3},
                schema_version=1,
                taken_at=NOW,
            )
        )


@pytest.mark.req("EF-BIB-043")
def test_revision_pending_waits_for_m2(engine: Engine, chain: dict[str, str]) -> None:
    table = acquisition.revision_pending
    with engine.begin() as connection:
        connection.execute(
            sa.insert(table).values(
                id=new_ulid(), variant_id=chain["variant"], revision=2, nature="rewrite"
            )
        )
        assert connection.execute(sa.select(table.c.state)).scalar_one() == "open"
        rejected(
            connection,
            sa.insert(table).values(
                id=new_ulid(), variant_id=chain["variant"], revision=3, nature="cosmetic"
            ),
            "ck_revision_pending_nature_values",
        )


# ------------------------------------------------------- contributors, ids


@pytest.mark.req("EF-BIB-046")
def test_contributor_is_an_entity_with_names_and_roles(
    engine: Engine, chain: dict[str, str]
) -> None:
    with engine.begin() as connection:
        person = new_ulid()
        connection.execute(
            sa.insert(catalog.contributor).values(
                id=person,
                kind="person",
                primary_name="Naoki Urasawa",
                birth_date="1960-01-02",
                birth_precision="day",
                death_date="2019",
                death_precision="year",
            )
        )
        for kind, name, lang in [
            ("native", "浦沢直樹", "ja"),
            ("romanized", "Urasawa Naoki", "ja-Latn"),
        ]:
            connection.execute(
                sa.insert(catalog.contributor_name).values(
                    contributor_id=person, name=name, lang=lang, kind=kind, family_first=True
                )
            )
        connection.execute(
            sa.insert(catalog.contribution).values(
                contributor_id=person,
                entity_type="work",
                entity_id=chain["work"],
                role="aut",
                position=0,
            )
        )
        rejected(
            connection,
            sa.insert(catalog.contribution).values(
                contributor_id=person, entity_type="work", entity_id=chain["work"], role="aut"
            ),
            "UNIQUE constraint failed",
        )
        rejected(
            connection,
            sa.insert(catalog.contribution).values(
                contributor_id=person, entity_type="chapter", entity_id=chain["work"], role="ill"
            ),
            "ck_contribution_entity_type_values",
        )
        rejected(
            connection,
            sa.insert(catalog.contribution).values(
                contributor_id=person, entity_type="work", entity_id=chain["work"], role="Author"
            ),
            "ck_contribution_role_marc_relator",
        )
        rejected(
            connection,
            sa.insert(catalog.contributor).values(id=new_ulid(), kind="band", primary_name="x"),
            "ck_contributor_kind_values",
        )
        rejected(
            connection,
            sa.insert(catalog.contributor_name).values(
                contributor_id=person, name="x", lang="ja", kind="sort-ish"
            ),
            "ck_contributor_name_kind_values",
        )


@pytest.mark.req("EF-BIB-003")
def test_external_id_is_polymorphic_and_unique_per_provider(
    engine: Engine, chain: dict[str, str]
) -> None:
    table = catalog.external_id
    with engine.begin() as connection:
        values = {
            "entity_type": "work",
            "entity_id": chain["work"],
            "provider": "anilist",
            "value": "30013",
            "origin": "provider",
        }
        connection.execute(sa.insert(table).values(**values))
        assert connection.execute(sa.select(table.c.state)).scalar_one() == "current"
        rejected(
            connection,
            sa.insert(table).values(**{**values, "entity_id": new_ulid()}),
            "UNIQUE constraint failed",
        )
        connection.execute(sa.insert(table).values(**{**values, "entity_type": "contributor"}))
        connection.execute(  # a renumbering adds, then marks the old one superseded
            sa.update(table).where(table.c.entity_type == "work").values(state="superseded")
        )
        for column, bad, constraint in [
            ("entity_type", "chapter", "ck_external_id_entity_type_values"),
            ("origin", "rumor", "ck_external_id_origin_values"),
            ("state", "gone", "ck_external_id_state_values"),
            ("provider", "Ani List", "ck_external_id_provider_key"),
        ]:
            rejected(
                connection,
                sa.insert(table).values(**{**values, "value": "other", column: bad}),
                constraint,
            )


@pytest.mark.req("EF-BIB-004")
def test_merge_suggestion_never_pairs_an_entity_with_itself(engine: Engine) -> None:
    table = catalog.merge_suggestion
    first, second = sorted((new_ulid(), new_ulid()))
    with engine.begin() as connection:
        values = {"entity_type": "work", "a": first, "b": second, "score": 0.8, "reason": "title"}
        connection.execute(sa.insert(table).values(id=new_ulid(), **values))
        assert connection.execute(sa.select(table.c.state)).scalar_one() == "open"
        rejected(  # same pair, same reason
            connection,
            sa.insert(table).values(id=new_ulid(), **values),
            "UNIQUE constraint failed",
        )
        connection.execute(
            sa.insert(table).values(
                id=new_ulid(),
                **{**values, "entity_type": "variant", "reason": "numbering_conflict"},
            )
        )
        rejected(  # unordered pairs would appear twice
            connection,
            sa.insert(table).values(
                id=new_ulid(), **{**values, "a": second, "b": first, "reason": "x"}
            ),
            "ck_merge_suggestion_pair_ordered",
        )


# --------------------------------------------------------------- hashes, kinds


@pytest.mark.req("ENF-SEC-008", "EF-BIB-048")
def test_hash_fields_prefixed_with_algorithm(engine: Engine, chain: dict[str, str]) -> None:
    """Catalog volet: a bare digest never says how it was computed."""
    with engine.begin() as connection:
        for bad in ("a" * 64, "md5:" + "a" * 32, "sha256:short", "sha256:" + "A" * 64):
            rejected(
                connection,
                sa.insert(catalog.variant_revision).values(
                    variant_id=chain["variant"], revision=7, seen_at=NOW, content_hash=bad
                ),
                "ck_variant_revision_content_hash_sha256_prefixed",
            )


@pytest.mark.req("EF-BIB-001")
def test_work_kinds_are_a_reference_table(engine: Engine) -> None:
    with engine.begin() as connection:
        rejected(
            connection,
            sa.insert(catalog.work).values(id=new_ulid(), kind="nope", title="t", sort_title="t"),
            "FOREIGN KEY constraint failed",
        )
        connection.execute(
            sa.insert(catalog.work_kind).values(
                key="zine", label_fr="Fanzine", default_direction="ltr", default_media="file"
            )
        )
        add_work(connection, kind="zine")
        rejected(
            connection,
            sa.insert(catalog.work_kind).values(key="x", label_fr="x", default_media="video"),
            "ck_work_kind_default_media_values",
        )


@pytest.mark.req("EF-BIB-010")
def test_work_titles_carry_a_language_and_a_kind(engine: Engine, chain: dict[str, str]) -> None:
    table = catalog.work_title
    with engine.begin() as connection:
        base = {"work_id": chain["work"], "title": "Monster", "lang": "en", "kind": "localized"}
        connection.execute(sa.insert(table).values(**base))
        rejected(connection, sa.insert(table).values(**base), "UNIQUE constraint failed")
        connection.execute(
            sa.insert(table).values(
                **{**base, "lang": "ja-Latn", "kind": "romanized", "script": "Latn"}
            )
        )
        rejected(
            connection,
            sa.insert(table).values(**{**base, "title": "x", "lang": "fr_FR"}),
            "ck_work_title_lang",
        )


@pytest.mark.req("ENF-OPS-021")
def test_normalizer_registry_versions(engine: Engine) -> None:
    table = catalog.normalizer
    with engine.begin() as connection:
        connection.execute(
            sa.insert(table).values(id="chapter-number", version=1, introduced_in="0.2.0")
        )
        connection.execute(
            sa.insert(table).values(id="chapter-number", version=2, introduced_in="0.3.0")
        )
        rejected(
            connection,
            sa.insert(table).values(id="chapter-number", version=2, introduced_in="0.3.0"),
            "UNIQUE constraint failed",
        )
        rejected(
            connection,
            sa.insert(table).values(id="chapter-number", version=0, introduced_in="0.1.0"),
            "ck_normalizer_version_min",
        )


@pytest.mark.req("EF-BIB-040")
def test_volume_is_a_numbered_closable_part_of_an_edition(
    engine: Engine, chain: dict[str, str]
) -> None:
    table = catalog.volume
    with engine.begin() as connection:
        volume = new_ulid()
        connection.execute(
            sa.insert(table).values(
                id=volume,
                edition_id=chain["edition"],
                number="3",
                number_sort=3.0,
                isbn="9782723489997",
            )
        )
        assert connection.execute(sa.select(table.c.closed)).scalar_one() is False
        rejected(
            connection,
            sa.insert(table).values(id=new_ulid(), edition_id=chain["edition"], number="3"),
            "UNIQUE constraint failed",
        )
        for bad in ("2723489997", "978272348999X", "97827234899970"):
            rejected(
                connection,
                sa.insert(table).values(
                    id=new_ulid(), edition_id=chain["edition"], number=bad[:2], isbn=bad
                ),
                "ck_volume_isbn_13_digits",
            )
        connection.execute(
            sa.update(catalog.unit)
            .where(catalog.unit.c.id == chain["unit"])
            .values(volume_id=volume)
        )


@pytest.mark.req("EF-BIB-050")
def test_variant_revision_cannot_be_overwritten_by_insert_or_replace(
    engine: Engine, chain: dict[str, str]
) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.insert(catalog.variant_revision).values(
                variant_id=chain["variant"], revision=1, seen_at=NOW, page_count=20
            )
        )
    for verb in ("INSERT OR REPLACE", "REPLACE"):
        with engine.begin() as connection:
            rejected(
                connection,
                sa.text(
                    f"{verb} INTO variant_revision (variant_id, revision, seen_at, page_count) "
                    f"VALUES ('{chain['variant']}', 1, '2026-11-04 12:00:00.000000', 1)"
                ),
                "append-only",
            )
    with engine.begin() as connection:
        assert (
            connection.execute(sa.select(catalog.variant_revision.c.page_count)).scalar_one() == 20
        )


@pytest.mark.req("EF-BIB-052")
def test_purge_deletes_variant_and_unit_despite_download_tickets(
    engine: Engine, chain: dict[str, str]
) -> None:
    """Erasure really deletes: tickets are closed first, then outlive their Variant."""
    table = acquisition.download_request
    with engine.begin() as connection:
        request, ranged = new_ulid(), new_ulid()
        connection.execute(
            sa.insert(table).values(
                id=request,
                target_kind="unit",
                variant_id=chain["variant"],
                source_binding_id=chain["binding"],
                ticket="t",
            )
        )
        connection.execute(
            sa.insert(table).values(
                id=ranged,
                target_kind="range",
                range_ref={"edition_id": chain["edition"]},
                source_binding_id=chain["binding"],
            )
        )
        connection.execute(
            sa.insert(acquisition.download_request_unit).values(
                download_request_id=ranged, unit_id=chain["unit"]
            )
        )
        connection.execute(
            sa.insert(acquisition.revision_pending).values(
                id=new_ulid(), variant_id=chain["variant"], revision=2, nature="rewrite"
            )
        )
        # a live unit ticket needs its Variant; closing it by purge releases the link
        rejected(
            connection,
            sa.update(table).where(table.c.id == request).values(variant_id=None),
            "ck_download_request_target_matches_kind",
        )
        connection.execute(
            sa.update(table).where(table.c.id == request).values(state="closed_by_purge")
        )
        connection.execute(sa.delete(catalog.unit_variant))
        connection.execute(sa.delete(catalog.unit))
        row = connection.execute(
            sa.select(table.c.state, table.c.variant_id).where(table.c.id == request)
        ).one()
        assert row == ("closed_by_purge", None)  # the ticket stays, closed on Shelfhold's side
        count = sa.select(sa.func.count())
        covered = acquisition.download_request_unit
        assert connection.execute(count.select_from(covered)).scalar_one() == 0
        assert connection.execute(count.select_from(acquisition.revision_pending)).scalar_one() == 0


@pytest.mark.req("EF-SRC-039")
def test_series_follow_filters_are_null_or_a_json_object(
    engine: Engine, chain: dict[str, str]
) -> None:
    follow = acquisition.series_follow
    with engine.begin() as connection:
        connection.execute(
            sa.insert(follow).values(
                edition_id=chain["edition"], work_id=chain["work"], filters={"lang": ["fr"]}
            )
        )
        for bad in ("[]", "3", '"x"', "not json", "null"):
            rejected(
                connection,
                sa.text("UPDATE series_follow SET filters = :f").bindparams(f=bad),
                "ck_series_follow_filters_json_object",
            )
        connection.execute(sa.update(follow).values(filters=None))
        stored = connection.execute(sa.select(sa.text("filters IS NULL")).select_from(follow))
        assert stored.scalar_one() == 1  # a real SQL NULL, not the JSON text 'null'


@pytest.mark.req("EF-BIB-028", "EF-BIB-035")
def test_team_priority_only_ranks_teams_of_its_own_edition(
    engine: Engine, chain: dict[str, str]
) -> None:
    with engine.begin() as connection:
        other_edition = add_edition(connection, chain["work"], lang="en")
        team = new_ulid()
        connection.execute(
            sa.insert(catalog.team).values(id=team, edition_id=other_edition, label="elsewhere")
        )
        rejected(
            connection,
            sa.insert(catalog.team_priority).values(
                edition_id=chain["edition"], team_id=team, rank=1
            ),
            "FOREIGN KEY constraint failed",
        )
        connection.execute(
            sa.insert(catalog.team_priority).values(edition_id=other_edition, team_id=team, rank=1)
        )
        connection.execute(sa.delete(catalog.team).where(catalog.team.c.id == team))
        remaining = connection.execute(
            sa.select(sa.func.count()).select_from(catalog.team_priority)
        )
        assert remaining.scalar_one() == 0  # cascades with its team


@pytest.mark.req("EF-BIB-049")
def test_blank_text_version_carries_no_reference(engine: Engine) -> None:
    with engine.begin() as connection:
        work_id = add_work(connection)
        for extra in ({"text_version_label": "abridged"},):
            rejected(
                connection,
                sa.insert(catalog.edition).values(**edition_values(work_id, **extra)),
                "ck_edition_text_version_has_reference",
            )
        translator = new_ulid()
        connection.execute(
            sa.insert(catalog.contributor).values(id=translator, kind="person", primary_name="T")
        )
        rejected(
            connection,
            sa.insert(catalog.edition).values(
                **edition_values(work_id, text_version_contributor_id=translator)
            ),
            "ck_edition_text_version_has_reference",
        )


@pytest.mark.req("EF-BIB-056")
def test_gap_ack_needs_a_number(engine: Engine, chain: dict[str, str]) -> None:
    with engine.begin() as connection:
        rejected(
            connection,
            sa.insert(acquisition.gap_ack).values(
                edition_id=chain["edition"], track="main", number_norm="", acked_at=NOW
            ),
            "ck_gap_ack_number_norm_not_empty",
        )


@pytest.mark.req("EF-BIB-028")
def test_one_preferred_variant_per_unit(engine: Engine, chain: dict[str, str]) -> None:
    table = catalog.unit_variant
    with engine.begin() as connection:
        second = add_variant(connection, chain["unit"], chain["binding"], "ch-1-bis")
        connection.execute(
            sa.update(table).where(table.c.id == chain["variant"]).values(preferred=True)
        )
        rejected(
            connection,
            sa.update(table).where(table.c.id == second).values(preferred=True),
            "UNIQUE constraint failed",
        )
        other_unit = add_unit(connection, chain["edition"], "2")
        third = add_variant(connection, other_unit, chain["binding"], "ch-2")
        connection.execute(sa.update(table).where(table.c.id == third).values(preferred=True))


@pytest.mark.req("EF-BIB-037")
def test_work_is_adult_flag_defaults_to_false(engine: Engine) -> None:
    with engine.begin() as connection:
        work_id = add_work(connection)
        flag = connection.execute(
            sa.select(catalog.work.c.is_adult).where(catalog.work.c.id == work_id)
        ).scalar_one()
        assert flag is False
        add_work(connection, is_adult=True)


@pytest.mark.req("EF-BIB-039")
def test_related_works_stay_distinct(engine: Engine) -> None:
    table = catalog.work_relation
    with engine.begin() as connection:
        connection.execute(
            sa.insert(catalog.work_relation_kind).values(key="adaptation_of", label_fr="Adaptation")
        )
        first, second = add_work(connection), add_work(connection)
        values = {
            "from_work_id": first,
            "to_work_id": second,
            "kind": "adaptation_of",
            "origin": "manual",
        }
        connection.execute(sa.insert(table).values(**values))
        rejected(connection, sa.insert(table).values(**values), "UNIQUE constraint failed")
        rejected(
            connection,
            sa.insert(table).values(**{**values, "to_work_id": first, "kind": "adaptation_of"}),
            "ck_work_relation_distinct_works",
        )
        rejected(
            connection,
            sa.insert(table).values(**{**values, "kind": "rival_of"}),
            "FOREIGN KEY constraint failed",
        )
