"""M4 L47 (intouchable : identité kosync): the export writes the alias of the file it produces.

A first export left no `document_alias` row (only an assembly or a rewrite did), so
`GET /kosync/syncs/progress/{digest}` of an exported file answered `{}`. The line is now written in
the transaction that completes the export's manifest line, with the hashes an e-reader and Kavita
identify the file by; a file that is the source byte for byte gets none; it follows the erasure of
its document (ADR-0053, ADR-0052) and is seen by no profile that cannot see the document.
"""

import hashlib
import io
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.export.application.export_alias import complete_hashes, record_export_alias
from shelfhold.infrastructure.schema import export, storage
from shelfhold.reading.domain.document_hash import kavita_hash, koreader_partial_md5
from tests.bundle_env import Chapter
from tests.export_env import export_env
from tests.integration.test_l47_kosync import (
    ABSENT_MD5,
    lab,  # noqa: F401  (fixture)
    library,
    positions,
    positions_user,
    pull,
    push,
    shape,
)
from tests.integration.test_l48_opds import World
from tests.integration.test_purge_now import run as purge_run
from tests.purge_env import make_purge_env
from tests.upgrade_env import export_one

pytestmark = pytest.mark.req("EF-LEC-009", "EF-LEC-014", "ENF-QUAL-005")


def aliases(engine: sa.Engine, item_id: str | None = None) -> list[sa.Row[tuple[object, ...]]]:
    query = sa.select(export.document_alias)
    if item_id is not None:
        query = query.where(export.document_alias.c.export_item_id == item_id)
    with engine.connect() as connection:
        return list(connection.execute(query))


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_first_export_writes_the_alias_of_the_file() -> None:
    with export_env() as env:
        exported = export_one(env)
        data = exported.target.read_bytes()
        with env.engine.connect() as connection:
            source = connection.execute(
                sa.select(storage.content.c.original_sha256)
                .select_from(
                    export.export_item.join(
                        storage.content, storage.content.c.id == export.export_item.c.content_id
                    )
                )
                .where(export.export_item.c.id == exported.export_item_id)
            ).scalar_one()
        assert sha(data) != source  # the export rendered the file: the precondition of the alias

        (row,) = aliases(env.engine, exported.export_item_id)
        assert row.file_hash == sha(data) == exported.old_file_hash
        assert row.shelf_destination_id == env.destination_id
        assert row.koreader_partial_md5 == "md5:" + koreader_partial_md5(io.BytesIO(data))
        assert row.kavita_md5 == "md5:" + kavita_hash(io.BytesIO(data)).lower()
        with env.engine.connect() as connection:  # what a position is anchored on
            anchor = connection.execute(
                sa.select(storage.content.c.content_hash)
                .select_from(
                    export.export_item.join(
                        storage.content, storage.content.c.id == export.export_item.c.content_id
                    )
                )
                .where(export.export_item.c.id == exported.export_item_id)
            ).scalar_one()
        assert row.content_hash == (anchor or exported.old_file_hash)


def test_replaying_the_export_keeps_a_single_alias() -> None:
    with export_env() as env:
        exported = export_one(env)
        again = env.pipeline.run(env.destination_id, job_id="01J00000000000000000000001")
        assert again.ops == ()  # nothing to write the second time
        with env.engine.begin() as connection:  # the line closed twice (a crash, a boot)
            for _ in range(2):
                record_export_alias(
                    connection, destination_id=env.destination_id,
                    export_item_id=exported.export_item_id, file_hash=exported.old_file_hash,
                    path=exported.target,
                )  # fmt: skip
        assert len(aliases(env.engine, exported.export_item_id)) == 1


def test_a_copy_identical_to_the_source_writes_no_alias() -> None:
    with export_env() as env:
        exported = export_one(env)
        with env.engine.begin() as connection:
            connection.execute(sa.delete(export.document_alias))
            connection.execute(  # the file is the source's bytes, as a passthrough would leave it
                sa.update(storage.content)
                .where(storage.content.c.id.in_(
                    sa.select(export.export_item.c.content_id).where(
                        export.export_item.c.id == exported.export_item_id
                    )
                ))
                .values(original_sha256=exported.old_file_hash)
            )  # fmt: skip
            written = record_export_alias(
                connection, destination_id=env.destination_id,
                export_item_id=exported.export_item_id, file_hash=exported.old_file_hash,
                path=exported.target,
            )  # fmt: skip
        assert written is False
        assert aliases(env.engine) == []


def test_a_file_that_cannot_be_hashed_leaves_the_export_and_a_row_to_heal(tmp_path: Path) -> None:
    """The export is never undone by its alias: the hashes stay empty for the next pass."""
    with export_env() as env:
        exported = export_one(env)
        with env.engine.begin() as connection:
            connection.execute(sa.delete(export.document_alias))
            written = record_export_alias(
                connection, destination_id=env.destination_id,
                export_item_id=exported.export_item_id, file_hash=exported.old_file_hash,
                path=tmp_path / "gone.cbz",
            )  # fmt: skip
        (row,) = aliases(env.engine)
        assert written is True and row.koreader_partial_md5 is None and row.kavita_md5 is None


def test_erasing_the_document_erases_its_alias() -> None:
    with export_env() as env:
        purge = make_purge_env(env.lib)
        exported = export_one(env)
        assert len(aliases(env.engine, exported.export_item_id)) == 1
        report = purge_run(purge, exported.unit_id)
        assert report.complete
        assert aliases(env.engine) == []


# -- the routes: what an e-reader finds through the alias, and who cannot ---------------------


def exported_digest(world: World, key: str) -> str:
    """A file the export placed, aliased by the code of the export (not by the test)."""
    item = library(world, key).add(Chapter.simple("1", count=20))
    data = item.path.read_bytes()
    with world.seed.engine.begin() as connection:
        assert record_export_alias(
            connection, destination_id=None, export_item_id=item.item_id, file_hash=sha(data),
            path=item.path,
        )  # fmt: skip
    complete_hashes(
        world.seed.engine, export_item_id=item.item_id, file_hash=sha(data), path=item.path
    )  # after the commit, as the export does
    return koreader_partial_md5(io.BytesIO(data))  # bare, as KOReader sends it


def test_the_e_reader_finds_the_exported_file_by_its_digest(lab: World) -> None:  # noqa: F811
    digest = exported_digest(lab, "ok")
    assert push(lab.mum, digest, "7", 7 / 20).status_code == 200
    assert pull(lab.mum, digest).json()["progress"] == "7"
    assert len(positions(lab)) == 1


def test_no_alias_is_visible_from_another_account(lab: World) -> None:  # noqa: F811
    """A child below the ceiling of the Edition, and an unknown digest, get the same answer."""
    digest = exported_digest(lab, "above")
    assert push(lab.mum, digest, "3", 3 / 20).status_code == 200  # the control: it exists
    assert shape(pull(lab.kid, digest)) == shape(pull(lab.kid, ABSENT_MD5))
    assert pull(lab.kid, digest).json() == {}
    assert {r.user_id for r in positions(lab)} == {positions_user(lab, "mum")}  # the adult's only


def test_a_file_written_by_a_binding_plan_gets_its_alias_and_a_second_pass_adds_none() -> None:
    """The other way a file leaves (the plan of a destination, run by the fan-out)."""
    from tests.integration.test_fan_out_plans import item_of, setup, write_plan

    with export_env() as env:
        fan, plans, contents, _probe = setup(env)
        series = env.new_series()
        one = item_of(env, series, "1", b"first volume " * 50, contents)
        plans.by_destination = {env.destination_id: write_plan(one)}
        for call in ("call-1", "call-2"):
            fan.run(env.shelf_id, call)
            (row,) = aliases(env.engine, one)
            data = next(iter(env.files())).read_bytes()
            assert row.file_hash == sha(data)
            assert row.koreader_partial_md5 == "md5:" + koreader_partial_md5(io.BytesIO(data))
            assert row.shelf_destination_id == env.destination_id
