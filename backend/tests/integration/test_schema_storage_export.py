"""M1a schema, storage / export / reading hooks (migration 0004): constraints that bite."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine, exc

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import (
    catalog,
    export,
    identity,
    metadata,
    platform,
    reading,
    storage,
)
from tests.integration.test_schema_catalog import (
    add_binding,
    add_edition,
    add_source_item,
    add_unit,
    add_variant,
    add_work,
)

ARGON = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"
SHA = "sha256:" + "a" * 64
SHA2 = "sha256:" + "b" * 64
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(tmp_path / "export.db")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


def rejected(connection: sa.Connection, statement: Any, expected: str) -> None:
    """The statement fails, and for the reason named: a constraint, or a message fragment."""
    with pytest.raises((exc.IntegrityError, exc.StatementError, exc.DatabaseError)) as raised:
        connection.execute(statement)
    assert expected in str(raised.value.orig if raised.value.orig else raised.value), raised.value


# ------------------------------------------------------------------- builders


def insert(connection: sa.Connection, table: sa.Table, **values: Any) -> str:
    """Insert a row (a ULID `id` is made up when the table has one) and return its key."""
    key = "id" if "id" in table.c else None
    if key and key not in values:
        values[key] = new_ulid()
    connection.execute(sa.insert(table).values(**values))
    return str(values[key]) if key else ""


def add_target(connection: sa.Connection, **extra: Any) -> str:
    return insert(connection, export.export_target, kind="kavita", name=f"k-{new_ulid()}", **extra)


def add_shelf(connection: sa.Connection, **extra: Any) -> str:
    return insert(connection, export.shelf, name=f"s-{new_ulid()}", phrase="Manga", **extra)


def add_destination(connection: sa.Connection, shelf_id: str, target_id: str, **extra: Any) -> str:
    values: dict[str, Any] = {
        "shelf_id": shelf_id,
        "target_id": target_id,
        "transport": "tree",
        "egress": "foyer",
        "root_subpath": "manga",
        "template": "{series}/{series} - {number}",
        "template_origin": "default@1",
        **extra,
    }
    return insert(connection, export.shelf_destination, **values)


def add_library(connection: sa.Connection, target_id: str, ref: str = "1") -> str:
    return insert(connection, export.remote_library, export_target_id=target_id, remote_ref=ref)


def link_library(connection: sa.Connection, destination: str, target: str, library: str) -> None:
    connection.execute(
        sa.insert(export.shelf_destination_remote_library).values(
            shelf_destination_id=destination, export_target_id=target, remote_library_id=library
        )
    )


def add_content(connection: sa.Connection, chain: dict[str, str], **extra: Any) -> str:
    values: dict[str, Any] = {
        "unit_id": chain["unit"],
        "variant_id": chain["variant"],
        "kind": "images",
        "media_type": "application/vnd.comicbook+zip",
        "format": "cbz",
        "retention": "keep",
        "path": "manga/ch-1.cbz",
        "original_sha256": SHA,
        "store_provenance": "received",
        "size": 1234,
        **extra,
    }
    return insert(connection, storage.content, **values)


def add_export_item(connection: sa.Connection, edition_id: str, **extra: Any) -> str:
    values: dict[str, Any] = {"kind": "unit", **extra}
    return insert(connection, export.export_item, edition_id=edition_id, **values)


def add_record(connection: sa.Connection, destination: str, item: str, **extra: Any) -> None:
    values: dict[str, Any] = {
        "shelf_destination_id": destination,
        "export_item_id": item,
        "locator_type": "path",
        "rel_path": "manga/Serie/Serie - 1.cbz",
        "state": "written",
        **extra,
    }
    connection.execute(sa.insert(export.export_record).values(**values))


@pytest.fixture
def chain(engine: Engine) -> dict[str, str]:
    """work > edition > binding > unit > variant, plus a shelf, a target and a destination."""
    with engine.begin() as connection:
        work_id = add_work(connection)
        edition_id = add_edition(connection, work_id)
        binding_id = add_binding(connection, work_id, edition_id, add_source_item(connection))
        unit_id = add_unit(connection, edition_id)
        variant_id = add_variant(connection, unit_id, binding_id)
        shelf_id = add_shelf(connection)
        target_id = add_target(connection)
        destination_id = add_destination(connection, shelf_id, target_id)
    return {
        "work": work_id,
        "edition": edition_id,
        "binding": binding_id,
        "unit": unit_id,
        "variant": variant_id,
        "shelf": shelf_id,
        "target": target_id,
        "destination": destination_id,
    }


# ---------------------------------------------------------------- identifiers

API_ID_TABLES = [
    (storage.content, "id"),
    (storage.trash_item, "id"),
    (storage.replica, "id"),
    (export.export_target, "id"),
    (export.shelf, "id"),
    (export.shelf_destination, "id"),
    (export.remote_library, "id"),
    (export.export_item, "id"),
    (export.export_series, "id"),
    (reading.reading_position, "id"),
]


@pytest.mark.req("ENF-OPS-020")
@pytest.mark.parametrize(("table", "column"), API_ID_TABLES, ids=lambda v: getattr(v, "name", v))
def test_api_ids_are_opaque_ulids(table: sa.Table, column: str) -> None:
    """Storage, export and reading volet of `test_api_ids_are_opaque_ulids`."""
    assert isinstance(table.c[column].type, sa.String)
    assert table.c[column].type.length == 26
    assert table.c[column].primary_key
    sql = {str(c.sqltext) for c in table.constraints if isinstance(c, sa.CheckConstraint)}
    assert any(column in text and "GLOB" in text for text in sql), sql


@pytest.mark.req("ENF-OPS-020")
def test_a_non_ulid_export_identifier_is_refused(engine: Engine) -> None:
    with engine.begin() as connection:
        for bad in ("1", "not-a-ulid", new_ulid().lower(), new_ulid() + "0"):
            rejected(
                connection,
                sa.insert(export.shelf).values(id=bad, name=f"s{bad}", phrase="p"),
                "ck_shelf_id_ulid",
            )


# ------------------------------------------------------------------- edition


@pytest.mark.req("EF-EXP-046")
def test_edition_shelf_id_is_nullable_and_points_at_a_shelf(
    engine: Engine, chain: dict[str, str]
) -> None:
    column = catalog.edition.c.shelf_id
    assert column.nullable
    assert {fk.target_fullname for fk in column.foreign_keys} == {"shelf.id"}
    with engine.begin() as connection:
        spare = add_shelf(connection)
        edition = catalog.edition
        assert (
            connection.execute(
                sa.select(edition.c.shelf_id).where(edition.c.id == chain["edition"])
            ).scalar_one()
            is None
        )  # an Edition without a shelf is "unshelved"
        rejected(
            connection,
            sa.update(edition).where(edition.c.id == chain["edition"]).values(shelf_id=new_ulid()),
            "FOREIGN KEY constraint failed",
        )
        connection.execute(
            sa.update(edition).where(edition.c.id == chain["edition"]).values(shelf_id=spare)
        )
        # deleting a shelf does not delete its Editions: they go back to "À ranger"
        connection.execute(sa.delete(export.shelf).where(export.shelf.c.id == spare))
        assert (
            connection.execute(
                sa.select(edition.c.shelf_id).where(edition.c.id == chain["edition"])
            ).scalar_one()
            is None
        )


# ------------------------------------------------------------------- content


@pytest.mark.req("EF-BIB-006", "EF-BIB-048")
def test_content_retention_decides_whether_there_is_a_store_path(
    engine: Engine, chain: dict[str, str]
) -> None:
    with engine.begin() as connection:
        rejected(
            connection,
            sa.insert(storage.content).values(
                id=new_ulid(), unit_id=chain["unit"], variant_id=chain["variant"], kind="images",
                media_type="application/pdf", format="pdf", retention="keep", path=None,
                original_sha256=SHA, store_provenance="received", size=1,
            ),
            "ck_content_path_iff_keep",
        )  # fmt: skip
        for retention in ("passthrough", "adopted_ref"):
            rejected(
                connection,
                sa.insert(storage.content).values(
                    id=new_ulid(), unit_id=chain["unit"], variant_id=chain["variant"],
                    kind="images", media_type="application/pdf", format="pdf",
                    retention=retention, path="x.pdf", original_sha256=SHA,
                    store_provenance="received", size=1,
                ),
                "ck_content_path_iff_keep",
            )  # fmt: skip
        add_content(connection, chain, retention="passthrough", path=None)


@pytest.mark.req("EF-BIB-006")
@pytest.mark.parametrize("bad", ["/etc/passwd", "../x.cbz", "a/../b.cbz", "a/.."])
def test_content_path_stays_relative_to_the_store(
    engine: Engine, chain: dict[str, str], bad: str
) -> None:
    with engine.begin() as connection:
        rejected(connection, sa.insert(storage.content).values(
            id=new_ulid(), unit_id=chain["unit"], variant_id=chain["variant"], kind="images",
            media_type="application/pdf", format="pdf", retention="keep", path=bad,
            original_sha256=SHA, store_provenance="received", size=1,
        ), "ck_content_path_relative")  # fmt: skip


@pytest.mark.req("EF-EXP-028", "EF-BIB-045")
def test_only_an_adopted_file_may_have_no_variant(engine: Engine, chain: dict[str, str]) -> None:
    with engine.begin() as connection:
        rejected(
            connection,
            sa.insert(storage.content).values(
                id=new_ulid(), kind="images", media_type="application/pdf", format="pdf",
                retention="keep", path="x.pdf", original_sha256=SHA,
                store_provenance="received", size=1,
            ),
            "ck_content_variant_unless_adopted",
        )  # fmt: skip
        insert(
            connection, storage.content, kind="images", media_type="application/pdf",
            format="pdf", retention="adopted_ref", original_sha256=SHA,
            store_provenance="received", size=1,
        )  # fmt: skip


@pytest.mark.req("EF-BIB-044")
def test_content_unit_is_the_unit_of_its_variant(engine: Engine, chain: dict[str, str]) -> None:
    with engine.begin() as connection:
        other_unit = add_unit(connection, chain["edition"], "2")
        rejected(
            connection,
            sa.insert(storage.content).values(
                id=new_ulid(), unit_id=other_unit, variant_id=chain["variant"], kind="images",
                media_type="application/pdf", format="pdf", retention="keep", path="x.pdf",
                original_sha256=SHA, store_provenance="received", size=1,
            ),
            "FOREIGN KEY constraint failed",
        )  # fmt: skip
        add_content(connection, chain)
        # a file that carries several Units is tied to its Variant only (unit_coverage says which)
        add_content(connection, chain, unit_id=None, media_type="application/pdf", path="x.pdf")


@pytest.mark.req("EF-EXP-058", "EF-EXP-046")
def test_a_variant_has_at_most_one_content_per_media_type(
    engine: Engine, chain: dict[str, str]
) -> None:
    with engine.begin() as connection:
        add_content(connection, chain)
        add_content(connection, chain, media_type="application/epub+zip", format="epub", path="e")
        rejected(
            connection,
            sa.insert(storage.content).values(
                id=new_ulid(), unit_id=chain["unit"], variant_id=chain["variant"], kind="images",
                media_type="application/vnd.comicbook+zip", format="cbz", retention="keep",
                path="other.cbz", original_sha256=SHA, store_provenance="received", size=1,
            ),
            "UNIQUE constraint failed",
        )  # fmt: skip


@pytest.mark.req("EF-BIB-051", "EF-BIB-006")
def test_content_derived_hash_and_processor_come_together(
    engine: Engine, chain: dict[str, str]
) -> None:
    with engine.begin() as connection:
        rejected(
            connection,
            sa.insert(storage.content).values(
                id=new_ulid(), unit_id=chain["unit"], variant_id=chain["variant"], kind="text",
                media_type="application/epub+zip", format="epub", retention="keep", path="e",
                original_sha256=SHA, store_provenance="extracted_derived", size=1,
                derived_hash=SHA2,
            ),
            "ck_content_derived_pair",
        )  # fmt: skip
        add_content(
            connection, chain, kind="text", media_type="application/epub+zip", format="epub",
            store_provenance="extracted_derived", derived_hash=SHA2,
            derived_by="xhtml-neutralizer@v1",
        )  # fmt: skip


@pytest.mark.req("ENF-SEC-008", "EF-BIB-048")
def test_hash_fields_prefixed_with_algorithm(engine: Engine, chain: dict[str, str]) -> None:
    """Storage and export volet: a bare digest never says how it was computed."""
    bad_values = ("a" * 64, "md5:" + "a" * 32, "sha256:short", "sha256:" + "A" * 64)
    with engine.begin() as connection:
        item = add_export_item(connection, chain["edition"])
        for bad in bad_values:
            for field in ("original_sha256", "content_hash", "derived_hash"):
                extra: dict[str, Any] = {field: bad}
                if field == "derived_hash":
                    extra["derived_by"] = "x@v1"
                with pytest.raises(exc.IntegrityError, match=f"ck_content_{field}_sha256"):
                    add_content(connection, chain, **extra)
            for field in ("content_hash", "file_hash"):
                with pytest.raises(exc.IntegrityError, match=f"ck_replica_{field}_sha256"):
                    insert(
                        connection, storage.replica, unit_id=chain["unit"], location="store",
                        rel_path="a", **{field: bad},
                    )  # fmt: skip
            for field in ("content_hash", "sidecar_sha256", "file_hash"):
                with pytest.raises(exc.IntegrityError, match=f"ck_export_record_{field}_sha256"):
                    add_record(connection, chain["destination"], item, **{field: bad})
            with pytest.raises(exc.IntegrityError, match="ck_document_alias_file_hash_sha256"):
                insert(
                    connection, export.document_alias, export_item_id=item, file_hash=bad,
                    content_hash=SHA,
                )  # fmt: skip


# ------------------------------------------------------------------- replica


@pytest.mark.req("EF-BIB-045")
def test_replica_belongs_to_a_unit_or_an_export_item(engine: Engine, chain: dict[str, str]) -> None:
    with engine.begin() as connection:
        rejected(
            connection,
            sa.insert(storage.replica).values(id=new_ulid(), location="store", rel_path="a"),
            "ck_replica_has_owner",
        )
        item = add_export_item(connection, chain["edition"])
        insert(connection, storage.replica, export_item_id=item, location="store", rel_path="a")


@pytest.mark.req("EF-BIB-045", "EF-EXP-059")
def test_replica_in_a_library_names_its_physical_root_and_path(
    engine: Engine, chain: dict[str, str]
) -> None:
    with engine.begin() as connection:
        root = new_ulid()
        connection.execute(
            sa.insert(platform.physical_root).values(root_id=root, path_entry="/library/manga")
        )
        for location in ("library", "adopted_ro"):
            rejected(
                connection,
                sa.insert(storage.replica).values(
                    id=new_ulid(), unit_id=chain["unit"], location=location, rel_path="a"
                ),
                "ck_replica_library_has_root_and_path",
            )
        insert(
            connection, storage.replica, unit_id=chain["unit"], location="library",
            physical_root_id=root, rel_path="a.cbz",
        )  # fmt: skip
        # the same file is never two replicas: it would count twice as an independent copy
        rejected(
            connection,
            sa.insert(storage.replica).values(
                id=new_ulid(),
                unit_id=chain["unit"],
                location="library",
                physical_root_id=root,
                rel_path="a.cbz",
            ),
            "UNIQUE constraint failed",
        )
        insert(connection, storage.replica, unit_id=chain["unit"], location="store", rel_path="s")
        rejected(
            connection,
            sa.insert(storage.replica).values(
                id=new_ulid(), unit_id=chain["unit"], location="store", rel_path="s"
            ),
            "UNIQUE constraint failed",
        )


@pytest.mark.req("EF-BIB-045", "EF-BIB-051")
def test_replica_defaults_are_the_cautious_ones(engine: Engine, chain: dict[str, str]) -> None:
    with engine.begin() as connection:
        key = insert(
            connection, storage.replica, unit_id=chain["unit"], location="upstream",
            remote_ref="chapter/12",
        )  # fmt: skip
        row = connection.execute(
            sa.select(storage.replica).where(storage.replica.c.id == key)
        ).one()
        assert row.independent is False  # never counted as a copy until proven (inode distinct)
        assert row.mutable_by_third_party is False
        assert row.fidelity == "original"
        rejected(
            connection,
            sa.update(storage.replica).where(storage.replica.c.id == key).values(fidelity="x"),
            "ck_replica_fidelity_values",
        )


# ---------------------------------------------------------------- trash_item


@pytest.mark.req("EF-BIB-021", "EF-EXP-059")
def test_trash_item_expires_after_it_was_deleted(engine: Engine) -> None:
    later = datetime(2026, 12, 4, 12, 0, tzinfo=UTC)
    with engine.begin() as connection:
        root = new_ulid()
        connection.execute(
            sa.insert(platform.physical_root).values(root_id=root, path_entry="/library/manga")
        )
        values: dict[str, Any] = {
            "kind": "export_file", "ref": new_ulid(), "path": "2026-11-04/a.cbz",
            "physical_root_id": root, "deleted_at": NOW,
        }  # fmt: skip
        insert(connection, storage.trash_item, purge_after=later, **values)
        rejected(
            connection,
            sa.insert(storage.trash_item).values(
                id=new_ulid(), purge_after=NOW.replace(year=2025), **values
            ),
            "ck_trash_item_purge_after_deleted_at",
        )
        rejected(
            connection,
            sa.insert(storage.trash_item).values(
                id=new_ulid(), **{**values, "kind": "series"}, purge_after=later
            ),
            "ck_trash_item_kind_values",
        )


# ------------------------------------------------------------------- targets


@pytest.mark.req("EF-EXP-047", "EF-EXP-060")
def test_export_target_path_map_is_a_list_of_mapped_roots(engine: Engine) -> None:
    with engine.begin() as connection:
        good = [{"local": "/library/manga", "remote": "/manga", "mode": "rw"}]
        insert(connection, export.export_target, kind="kavita", name="a", path_map=good)
        for bad in ({"local": "/x"}, "text"):
            rejected(
                connection,
                sa.insert(export.export_target).values(
                    id=new_ulid(), kind="kavita", name=f"b{bad}", path_map=bad
                ),
                "ck_export_target_path_map_list",
            )
        rejected(
            connection,
            sa.insert(export.export_target).values(
                id=new_ulid(), kind="kavita", name="c", access_partitioning="room"
            ),
            "ck_export_target_access_partitioning_values",
        )
        rejected(
            connection,
            sa.insert(export.export_target).values(id=new_ulid(), kind="kavita", name="a"),
            "UNIQUE constraint failed",
        )


@pytest.mark.req("ENF-SEC-005", "ENF-SEC-013")
def test_export_target_secrets_are_a_sealed_envelope_or_nothing(engine: Engine) -> None:
    with engine.begin() as connection:
        rejected(
            connection,
            sa.insert(export.export_target).values(
                id=new_ulid(), kind="kavita", name="half", secret_ct=b"x" * 32
            ),
            "ck_export_target_secret_sealed",
        )


@pytest.mark.req("EF-EXP-058", "EF-EXP-047")
def test_shelf_destination_declares_transport_and_egress(
    engine: Engine, chain: dict[str, str]
) -> None:
    with engine.begin() as connection:
        for field, bad in (("transport", "ftp"), ("egress", "cloud"), ("mode", "broken")):
            values: dict[str, Any] = {
                "id": new_ulid(), "shelf_id": chain["shelf"], "target_id": chain["target"],
                "transport": "tree", "egress": "foyer", "root_subpath": "other", "template": "t",
                "template_origin": "default@1", field: bad,
            }  # fmt: skip
            rejected(
                connection,
                sa.insert(export.shelf_destination).values(**values),
                f"ck_shelf_destination_{field}_values",
            )
        rejected(
                connection,
                sa.insert(export.shelf_destination).values(
                    id=new_ulid(), shelf_id=chain["shelf"], target_id=chain["target"],
                    transport="tree", egress="foyer", root_subpath="other", template="t",
                    template_origin="default@1", **{field: bad},
                ),
                f"ck_shelf_destination_{field}_values",
            )  # fmt: skip
        rejected(
            connection,
            sa.insert(export.shelf_destination).values(
                id=new_ulid(), shelf_id=chain["shelf"], target_id=chain["target"],
                transport="tree", egress="foyer", root_subpath="manga", template="t",
                template_origin="default@1",
            ),
            "UNIQUE constraint failed",
        )  # fmt: skip
        row = connection.execute(
            sa.select(export.shelf_destination).where(
                export.shelf_destination.c.id == chain["destination"]
            )
        ).one()
        assert row.import_mode == "copy"
        assert row.mode == "normal"
        assert row.adopted is False
        assert row.enabled is True
        assert row.kavita_series_age_locked is False
        assert row.sync_progress is False


@pytest.mark.req("EF-EXP-050", "EF-USR-015", "EF-EXP-048")
def test_shelf_policy_columns(engine: Engine) -> None:
    with engine.begin() as connection:
        key = add_shelf(
            connection, max_age_rating=12, allowed_profiles=["01ARZ3NDEKTSV4RRFFQ69G5FAV"]
        )
        row = connection.execute(sa.select(export.shelf).where(export.shelf.c.id == key)).one()
        assert row.retention_mode == "passthrough"  # ADR-0028: the default of connected services
        assert row.purge_upstream_after_export is False
        assert row.hold_until_rated is False
        assert row.follow_suspended == []
        assert row.format_policy == "cbz_per_chapter"
        for field, bad in (("retention_mode", "forever"), ("format_policy", "zip")):
            rejected(
                connection,
                sa.insert(export.shelf).values(
                    id=new_ulid(), name=f"x{bad}", phrase="p", **{field: bad}
                ),
                f"ck_shelf_{field}_values",
            )
        rejected(
            connection,
            sa.insert(export.shelf).values(id=new_ulid(), name="y", phrase="p", max_age_rating=-1),
            "ck_shelf_max_age_rating_min_age",
        )
        rejected(
            connection,
            sa.insert(export.shelf).values(
                id=new_ulid(), name="z", phrase="p", allowed_profiles={"a": 1}
            ),
            "ck_shelf_allowed_profiles_list",
        )


# ------------------------------------------------------------- remote library


@pytest.mark.req("EF-EXP-062")
def test_remote_library_local_ulid_no_cross_server_collision(engine: Engine) -> None:
    """Two servers whose libraries have the same remote id never collide."""
    with engine.begin() as connection:
        first, second = add_target(connection), add_target(connection)
        a, b = add_library(connection, first, "1"), add_library(connection, second, "1")
        assert a != b  # local identifiers, one per (server, remote library)
        rejected(
            connection,
            sa.insert(export.remote_library).values(
                id=new_ulid(), export_target_id=first, remote_ref="1"
            ),
            "UNIQUE constraint failed",
        )
        # the remote reference is a cache: it can be re-resolved without changing the identity
        connection.execute(
            sa.update(export.remote_library)
            .where(export.remote_library.c.id == a)
            .values(remote_ref="7")
        )
        rejected(
            connection,
            sa.insert(export.remote_library).values(
                id=new_ulid(), export_target_id=first, remote_ref=""
            ),
            "ck_remote_library_remote_ref_not_empty",
        )


@pytest.mark.req("EF-EXP-062", "EF-EXP-055")
def test_a_destination_only_links_libraries_of_its_own_server(
    engine: Engine, chain: dict[str, str]
) -> None:
    with engine.begin() as connection:
        mine = add_library(connection, chain["target"], "1")
        foreign = add_library(connection, add_target(connection), "1")
        link_library(connection, chain["destination"], chain["target"], mine)
        rejected(
            connection,
            sa.insert(export.shelf_destination_remote_library).values(
                shelf_destination_id=chain["destination"],
                export_target_id=chain["target"],
                remote_library_id=foreign,
            ),
            "FOREIGN KEY constraint failed",
        )
        # the audit of a library (EF-EXP-055) is per library of the server
        insert_audit = sa.insert(export.kavita_access_audit).values(
            remote_library_id=mine, accounts=[], exposed_to_children=False, audited_at=NOW
        )
        connection.execute(insert_audit)
        rejected(connection, insert_audit, "UNIQUE constraint failed")


@pytest.mark.req("EF-EXP-039")
def test_kavita_instance_is_one_row_per_server(engine: Engine, chain: dict[str, str]) -> None:
    with engine.begin() as connection:
        values = {"target_id": chain["target"], "fingerprint": "f", "seen_at": NOW}
        connection.execute(sa.insert(export.kavita_instance).values(**values))
        rejected(connection, sa.insert(export.kavita_instance).values(**values), "UNIQUE")


# -------------------------------------------------------------- export series


@pytest.fixture
def library(engine: Engine, chain: dict[str, str]) -> str:
    with engine.begin() as connection:
        library_id = add_library(connection, chain["target"], "1")
        link_library(connection, chain["destination"], chain["target"], library_id)
    return library_id


def add_series(
    connection: sa.Connection,
    chain: dict[str, str],
    library: str,
    name: str = "serie",
    **extra: Any,
) -> str:
    return insert(
        connection, export.export_series, shelf_destination_id=chain["destination"],
        remote_library_id=library, normalized_name=name, name_raw=name.title(), **extra,
    )  # fmt: skip


@pytest.mark.req("EF-EXP-032", "EF-EXP-062")
def test_export_series_name_is_unique_per_remote_library(
    engine: Engine, chain: dict[str, str], library: str
) -> None:
    with engine.begin() as connection:
        add_series(connection, chain, library, "one-piece")
        rejected(
            connection,
            sa.insert(export.export_series).values(
                id=new_ulid(), shelf_destination_id=chain["destination"],
                remote_library_id=library, normalized_name="one-piece", name_raw="One Piece",
            ),
            "UNIQUE constraint failed",
        )  # fmt: skip
        add_series(connection, chain, library, "naruto")


@pytest.mark.req("EF-EXP-062")
def test_export_series_library_is_linked_to_its_destination(
    engine: Engine, chain: dict[str, str], library: str
) -> None:
    with engine.begin() as connection:
        unlinked = add_library(connection, chain["target"], "2")
        rejected(
            connection,
            sa.insert(export.export_series).values(
                id=new_ulid(), shelf_destination_id=chain["destination"],
                remote_library_id=unlinked, normalized_name="x", name_raw="X",
            ),
            "FOREIGN KEY constraint failed",
        )  # fmt: skip


@pytest.mark.req("EF-EXP-052", "EF-EXP-057")
def test_adopted_series_freeze_their_values_together(
    engine: Engine, chain: dict[str, str], library: str
) -> None:
    with engine.begin() as connection:
        rejected(
            connection,
            sa.insert(export.export_series).values(
                id=new_ulid(), shelf_destination_id=chain["destination"],
                remote_library_id=library, normalized_name="adopted", name_raw="Adopted",
                frozen_series="Adopted",
            ),
            "ck_export_series_frozen_with_date",
        )  # fmt: skip
        add_series(
            connection, chain, library, "adopted", frozen_series="Adopted", frozen_language="fr",
            frozen_series_sort="adopted", frozen_localized_series="Adopté", frozen_at=NOW,
            has_volume=True, series_dir="Adopted", kavita_series_id="42",
        )  # fmt: skip
        rejected(
            connection,
            sa.insert(export.export_series).values(
                id=new_ulid(), shelf_destination_id=chain["destination"],
                remote_library_id=library, normalized_name="bad-lang", name_raw="B",
                frozen_series="B", frozen_at=NOW, frozen_language="all",
            ),
            "ck_export_series_frozen_language",
        )  # fmt: skip


@pytest.mark.req("EF-EXP-062")
def test_merge_then_split_keeps_export_series_stable(
    engine: Engine, chain: dict[str, str], library: str
) -> None:
    """Schema volet: an Edition is tied to ONE series per library, by its own row."""
    with engine.begin() as connection:
        one = add_series(connection, chain, library, "one")
        two = add_series(connection, chain, library, "two")
        table = export.edition_export_series
        values = {
            "edition_id": chain["edition"], "remote_library_id": library,
            "export_series_id": one, "assigned_at": NOW, "assigned_by": "first_export",
        }  # fmt: skip
        connection.execute(sa.insert(table).values(**values))
        rejected(connection, sa.insert(table).values(**values), "UNIQUE constraint failed")
        # a reassignment changes this link only; the series keep their identity
        connection.execute(sa.update(table).values(export_series_id=two, assigned_by="plan"))
        for key in (one, two):
            assert (
                connection.execute(
                    sa.select(export.export_series.c.id).where(export.export_series.c.id == key)
                ).scalar_one()
                == key
            )
        rejected(
            connection,
            sa.update(table).values(assigned_by="x"),
            "ck_edition_export_series_assigned_by_values",
        )


@pytest.mark.req("EF-EXP-062")
def test_edition_export_series_series_belongs_to_the_same_library(
    engine: Engine, chain: dict[str, str], library: str
) -> None:
    with engine.begin() as connection:
        other = add_library(connection, chain["target"], "2")
        link_library(connection, chain["destination"], chain["target"], other)
        series_elsewhere = add_series(connection, chain, other, "elsewhere")
        rejected(
            connection,
            sa.insert(export.edition_export_series).values(
                edition_id=chain["edition"], remote_library_id=library,
                export_series_id=series_elsewhere, assigned_at=NOW, assigned_by="first_export",
            ),
            "FOREIGN KEY constraint failed",
        )  # fmt: skip


@pytest.mark.req("EF-EXP-057", "EF-EXP-040")
def test_field_owner_is_per_series_of_its_own_destination(
    engine: Engine, chain: dict[str, str], library: str
) -> None:
    with engine.begin() as connection:
        series = add_series(connection, chain, library)
        other_destination = add_destination(
            connection, chain["shelf"], chain["target"], root_subpath="books"
        )
        values = {
            "export_series_id": series,
            "field": "age_rating",
            "last_value": 12,
            "pushed_at": NOW,
        }
        connection.execute(
            sa.insert(export.field_owner).values(
                shelf_destination_id=chain["destination"], **values
            )
        )
        rejected(
            connection,
            sa.insert(export.field_owner).values(shelf_destination_id=other_destination, **values),
            "FOREIGN KEY constraint failed",
        )
        rejected(
            connection,
            sa.insert(export.field_owner).values(
                shelf_destination_id=chain["destination"], **values
            ),
            "UNIQUE constraint failed",
        )
        # pushed_at empty: invalidated, to be pushed again after a reinstall of the server
        connection.execute(sa.update(export.field_owner).values(pushed_at=None))


# ------------------------------------------------- export items and coverage


@pytest.mark.req("EF-EXP-061", "EF-BIB-044")
def test_export_item_freezes_its_attributes_and_points_at_its_content(
    engine: Engine, chain: dict[str, str]
) -> None:
    with engine.begin() as connection:
        content = add_content(connection, chain)
        item = add_export_item(
            connection, chain["edition"], content_id=content, frozen_attrs={"series": "S"}
        )
        row = connection.execute(
            sa.select(export.export_item).where(export.export_item.c.id == item)
        ).one()
        assert row.frozen_attrs == {"series": "S"}
        assert row.closed is False
        rejected(
            connection,
            sa.insert(export.export_item).values(
                id=new_ulid(), edition_id=chain["edition"], kind="unit", frozen_attrs=[1]
            ),
            "ck_export_item_frozen_attrs_object",
        )
        rejected(
            connection,
            sa.insert(export.export_item).values(
                id=new_ulid(), edition_id=chain["edition"], kind="chapter"
            ),
            "ck_export_item_kind_values",
        )
        # erasing the content leaves the exported item, now without a stored file
        connection.execute(sa.delete(storage.content).where(storage.content.c.id == content))
        assert (
            connection.execute(
                sa.select(export.export_item.c.content_id).where(export.export_item.c.id == item)
            ).scalar_one()
            is None
        )


@pytest.mark.req("EF-LEC-014")
def test_unit_coverage_locators_nullable_and_filled_when_known(
    engine: Engine, chain: dict[str, str]
) -> None:
    table = export.unit_coverage
    with engine.begin() as connection:
        item = add_export_item(connection, chain["edition"], kind="volume")
        second = add_unit(connection, chain["edition"], "2")
        known = {"page": 12}
        connection.execute(
            sa.insert(table).values(
                carrier_type="export_item",
                carrier_id=item,
                unit_id=chain["unit"],
                position=0,
                extent="full",
                origin="source_name",
            )
        )  # unknown bounds: both NULL
        connection.execute(
            sa.insert(table).values(
                carrier_type="export_item",
                carrier_id=item,
                unit_id=second,
                position=1,
                start_locator=known,
                end_locator={"page": 30},
                extent="partial",
                origin="comicinfo",
                confidence=0.8,
            )
        )
        rows = connection.execute(
            sa.select(table.c.unit_id, table.c.start_locator, table.c.end_locator).order_by(
                table.c.position
            )
        ).all()
        assert [(r.start_locator, r.end_locator) for r in rows] == [
            (None, None),
            (known, {"page": 30}),
        ]
        rejected(
            connection,
            sa.insert(table).values(
                carrier_type="export_item", carrier_id=item, unit_id=second, position=2,
                extent="full", origin="manual",
            ),
            "UNIQUE constraint failed",
        )  # fmt: skip
        for field, bad in (
            ("carrier_type", "page"), ("extent", "half"), ("origin", "guess"), ("confidence", 2),
        ):  # fmt: skip
            values: dict[str, Any] = {
                "carrier_type": "export_item", "carrier_id": new_ulid(), "unit_id": chain["unit"],
                "position": 0, "extent": "full", "origin": "manual", field: bad,
            }  # fmt: skip
            with pytest.raises(exc.IntegrityError, match=f"ck_unit_coverage_{field}"):
                connection.execute(sa.insert(table).values(**values))


@pytest.mark.req("EF-BIB-044")
def test_coverage_follows_its_carrier_and_its_unit(engine: Engine, chain: dict[str, str]) -> None:
    table = export.unit_coverage
    count = sa.select(sa.func.count()).select_from(table)
    with engine.begin() as connection:
        item = add_export_item(connection, chain["edition"])
        content = add_content(connection, chain)
        for carrier_type, carrier_id in (("export_item", item), ("content", content)):
            connection.execute(
                sa.insert(table).values(
                    carrier_type=carrier_type,
                    carrier_id=carrier_id,
                    unit_id=chain["unit"],
                    position=0,
                    extent="full",
                    origin="source_name",
                )
            )
        assert connection.execute(count).scalar_one() == 2
        connection.execute(sa.delete(export.export_item).where(export.export_item.c.id == item))
        assert connection.execute(count).scalar_one() == 1  # no coverage left for a ghost item
        connection.execute(sa.delete(storage.content).where(storage.content.c.id == content))
        assert connection.execute(count).scalar_one() == 0
        item = add_export_item(connection, chain["edition"])
        ghost = add_unit(connection, chain["edition"], "9")
        connection.execute(
            sa.insert(table).values(
                carrier_type="export_item",
                carrier_id=item,
                unit_id=ghost,
                position=0,
                extent="full",
                origin="manual",
            )
        )
        # the Unit's erasure takes its coverage with it (EF-BIB-052)
        connection.execute(sa.delete(catalog.unit).where(catalog.unit.c.id == ghost))
        assert connection.execute(count).scalar_one() == 0


# ------------------------------------------------------------------- manifest


@pytest.mark.req("EF-EXP-058", "EF-EXP-039")
def test_manifest_keyed_by_shelf_destination_and_export_item(
    engine: Engine, chain: dict[str, str]
) -> None:
    """Schema volet: one record per (destination, item); the item is independent of both."""
    with engine.begin() as connection:
        other = add_destination(connection, chain["shelf"], add_target(connection))
        item = add_export_item(connection, chain["edition"])
        add_record(connection, chain["destination"], item)
        add_record(connection, other, item)  # the same item, another destination: its own record
        rejected(
            connection,
            sa.insert(export.export_record).values(
                shelf_destination_id=chain["destination"], export_item_id=item,
                locator_type="path", rel_path="elsewhere.cbz", state="written",
            ),
            "UNIQUE constraint failed",
        )  # fmt: skip
        assert list(export.export_record.primary_key.columns.keys()) == [
            "shelf_destination_id",
            "export_item_id",
        ]


@pytest.mark.req("EF-EXP-039", "EF-EXP-058")
def test_a_path_resolves_to_one_live_record_per_destination(
    engine: Engine, chain: dict[str, str]
) -> None:
    with engine.begin() as connection:
        first = add_export_item(connection, chain["edition"])
        second = add_export_item(connection, chain["edition"])
        add_record(connection, chain["destination"], first, state="recycled")
        add_record(connection, chain["destination"], second)  # a recycled path can be reused
        third = add_export_item(connection, chain["edition"])
        rejected(
            connection,
            sa.insert(export.export_record).values(
                shelf_destination_id=chain["destination"], export_item_id=third,
                locator_type="path", rel_path="manga/Serie/Serie - 1.cbz", state="planned",
            ),
            "UNIQUE constraint failed",
        )  # fmt: skip


@pytest.mark.req("EF-EXP-058")
def test_record_locator_is_typed_by_the_transport(engine: Engine, chain: dict[str, str]) -> None:
    with engine.begin() as connection:
        item = add_export_item(connection, chain["edition"])
        wrong = (
            {"locator_type": "path", "rel_path": None},
            {"locator_type": "remote_id", "rel_path": "a"},
            {"locator_type": "remote_id", "rel_path": None},
            {"locator_type": "idempotency_key", "rel_path": None, "remote_id": "x"},
            {"locator_type": "none", "rel_path": None, "idempotency_key": "k"},
            {"locator_type": "tree", "rel_path": "a"},
        )
        for values in wrong:
            with pytest.raises(exc.IntegrityError, match="ck_export_record_locator"):
                add_record(connection, chain["destination"], item, **values)
        add_record(connection, chain["destination"], item, locator_type="none", rel_path=None)
        for index, kind in enumerate(("remote_id", "idempotency_key")):
            other = add_export_item(connection, chain["edition"])
            add_record(
                connection, chain["destination"], other,
                locator_type=kind, rel_path=None, **{kind: f"v{index}"},
            )  # fmt: skip
        rejected(
            connection,
            sa.insert(export.export_record).values(
                shelf_destination_id=chain["destination"],
                export_item_id=new_ulid(),
                locator_type="none",
                state="exploded",
            ),
            "ck_export_record_state_values",
        )


@pytest.mark.req("EF-EXP-043", "EF-EXP-039")
def test_record_remembers_what_it_wrote(engine: Engine, chain: dict[str, str]) -> None:
    with engine.begin() as connection:
        item = add_export_item(connection, chain["edition"])
        add_record(
            connection, chain["destination"], item, size=10, mtime_ns=1_730_000_000_123_456_789,
            content_hash=SHA, file_hash=SHA2, sidecar_sha256=SHA, template_ref="default@1",
            format="cbz", remote_ref="series/3", written_by_shelfhold_at=NOW, exported_at=NOW,
        )  # fmt: skip
        row = connection.execute(sa.select(export.export_record)).one()
        assert row.mtime_ns == 1_730_000_000_123_456_789  # nanoseconds: an exact comparison
        assert row.content_hash != row.file_hash  # the payload and the file rendered for it


# ------------------------------------------------------------ document alias


@pytest.mark.req("EF-LEC-009", "EF-LEC-014")
def test_document_alias_keyed_by_export_item(engine: Engine, chain: dict[str, str]) -> None:
    table = export.document_alias
    assert "export_item_id" in table.c
    assert "unit_id" not in table.c  # before passe 7 the alias pointed at a Unit
    with engine.begin() as connection:
        item = add_export_item(connection, chain["edition"])
        other = add_export_item(connection, chain["edition"])
        # one row per rewrite of the exported file
        for file_hash in (SHA, SHA2):
            insert(
                connection, table, export_item_id=item, file_hash=file_hash, content_hash=SHA,
                koreader_partial_md5="md5:" + "c" * 32,
            )  # fmt: skip
        insert(connection, table, export_item_id=other, file_hash=SHA, content_hash=SHA)  # shared
        rejected(
            connection,
            sa.insert(table).values(export_item_id=item, file_hash=SHA, content_hash=SHA),
            "UNIQUE constraint failed",
        )
        rejected(
            connection,
            sa.insert(table).values(
                export_item_id=item,
                file_hash=SHA2.replace("b", "d"),
                content_hash=SHA,
                koreader_partial_md5="c" * 32,
            ),
            "ck_document_alias_koreader_partial_md5_md5_prefixed",
        )
        rejected(
            connection,
            sa.insert(table).values(export_item_id=new_ulid(), file_hash=SHA, content_hash=SHA),
            "FOREIGN KEY constraint failed",
        )


# ------------------------------------------------------------------- reading


def add_position(connection: sa.Connection, user_id: str, carrier: str, **extra: Any) -> str:
    values: dict[str, Any] = {
        "user_id": user_id,
        "carrier_type": "export_item",
        "carrier_id": carrier,
        "content_hash": SHA,
        "locator": {"progression": 0.5},
        "percent": 0.5,
        "client_ts": NOW,
        "server_ts": NOW,
        **extra,
    }
    return insert(connection, reading.reading_position, **values)


@pytest.fixture
def user(engine: Engine) -> str:
    user_id = new_ulid()
    with engine.begin() as connection:
        connection.execute(
            sa.insert(identity.user).values(
                id=user_id, username="reader", role="admin", can_rate=True,
                password_hash=ARGON,
            )
        )  # fmt: skip
    return user_id


@pytest.mark.req("EF-LEC-014", "EF-LEC-007")
def test_reading_position_anchors_on_a_file_not_a_unit(
    engine: Engine, chain: dict[str, str], user: str
) -> None:
    table = reading.reading_position
    assert "unit_id" not in table.c
    with engine.begin() as connection:
        item = add_export_item(connection, chain["edition"])
        add_position(connection, user, item)
        add_position(connection, user, item, percent=0.9)  # history is kept
        add_position(connection, user, new_ulid(), carrier_type="content")  # not exported (AA5)
        assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 3
        for field, bad in (
            ("carrier_type", "unit"), ("percent", 1.5), ("content_hash", "a" * 64),
        ):  # fmt: skip
            with pytest.raises(exc.IntegrityError, match=f"ck_reading_position_{field}"):
                add_position(connection, user, item, **{field: bad})
        rejected(
            connection,
            sa.insert(table).values(
                id=new_ulid(), user_id=new_ulid(), carrier_type="export_item", carrier_id=item,
                content_hash=SHA, locator={}, client_ts=NOW, server_ts=NOW,
            ),
            "FOREIGN KEY constraint failed",
        )  # fmt: skip
        # a position survives the file it was read on (resolved again by content_hash)
        connection.execute(sa.delete(export.export_item).where(export.export_item.c.id == item))
        assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 3
        # erasing the reader erases the reading history (EF-USR-013)
        connection.execute(sa.delete(identity.user).where(identity.user.c.id == user))
        assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0


@pytest.mark.req("EF-LEC-014", "EF-BIB-026")
def test_reading_progress_is_a_recomputable_projection(
    engine: Engine, chain: dict[str, str], user: str
) -> None:
    coverage = export.unit_coverage
    progress = sa.table(
        "reading_progress",
        sa.column("user_id"), sa.column("unit_id"), sa.column("state"),
        sa.column("last_position_id"), sa.column("approximate"),
    )  # fmt: skip
    with engine.begin() as connection:
        units = [chain["unit"], add_unit(connection, chain["edition"], "2"),
                 add_unit(connection, chain["edition"], "3")]  # fmt: skip
        volume = add_export_item(connection, chain["edition"], kind="volume")
        alone = add_export_item(connection, chain["edition"])
        lone_unit = add_unit(connection, chain["edition"], "4")
        for position, unit in enumerate(units):
            connection.execute(
                sa.insert(coverage).values(
                    carrier_type="export_item",
                    carrier_id=volume,
                    unit_id=unit,
                    position=position,
                    extent="full",
                    origin="source_name",
                )
            )
        connection.execute(
            sa.insert(coverage).values(
                carrier_type="export_item",
                carrier_id=alone,
                unit_id=lone_unit,
                position=0,
                extent="full",
                origin="source_name",
            )
        )
        # halfway through a volume of three Units: the first is read, the second under way
        latest = add_position(connection, user, volume, percent=0.5, server_ts=NOW)
        add_position(connection, user, volume, percent=0.1, server_ts=NOW.replace(hour=1))
        got = {
            r.unit_id: r
            for r in connection.execute(sa.select(progress).where(progress.c.user_id == user))
        }
        assert {u: got[u].state for u in units} == {
            units[0]: "read", units[1]: "in_progress", units[2]: "unread",
        }  # fmt: skip
        assert all(got[u].approximate for u in units)  # bounds unknown: proportional
        assert {got[u].last_position_id for u in units} == {latest}
        # a file that carries one Unit gives an exact answer
        add_position(connection, user, alone, percent=1.0)
        lone = connection.execute(sa.select(progress).where(progress.c.unit_id == lone_unit)).one()
        assert lone.state == "read"
        assert not lone.approximate
        # known bounds (pages) make it exact
        connection.execute(
            sa.update(coverage)
            .where(coverage.c.unit_id == units[1])
            .values(start_locator={"page": 10}, end_locator={"page": 20})
        )
        add_position(
            connection, user, volume, locator={"page": 15}, percent=0.5,
            server_ts=NOW.replace(hour=23),
        )  # fmt: skip
        exact = connection.execute(sa.select(progress).where(progress.c.unit_id == units[1])).one()
        assert exact.state == "in_progress"
        assert not exact.approximate
