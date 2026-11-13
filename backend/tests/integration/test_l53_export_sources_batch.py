"""M3 L53 (ENF-PERF-007): the export reads the library copies of a destination's Units in a few
statements, not one per Unit, and reads the same copies in the same order.

The bench (`tests/bench/bench_contention.py`) found that an export re-reads the whole destination
for each chapter, and that one compiled statement per Unit kept the interpreter from answering the
API. The batch must change the cost, never the answer: a copy found damaged is still never the
source, the first healthy copy by replica id is still the one, and a Unit without a copy is still
a Unit without a path.
"""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import storage
from shelfhold.library.adapters.export_source import export_sources
from shelfhold.library.replica import library_copies, library_copies_of, record_replica
from tests.library_env import LibEnv, lib_env


def sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def put_copy(connection: sa.Connection, lib: LibEnv, unit_id: str, name: str) -> str:
    return record_replica(
        connection, unit_id=unit_id, location="library", content_hash=sha(unit_id),
        file_hash=sha(name), independent=True, physical_root_id=lib.library_root,
        rel_path=name,
    )  # fmt: skip


def damage(connection: sa.Connection, replica_id: str) -> None:
    connection.execute(
        sa.text("UPDATE replica SET damaged_at = '2026-11-01 00:00:00' WHERE id = :i"),
        {"i": replica_id},
    )


@contextmanager
def statements(engine: sa.Engine) -> Iterator[list[str]]:
    seen: list[str] = []

    def count(_c: object, _cur: object, statement: str, *_a: object) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append(statement)

    sa.event.listen(engine, "before_cursor_execute", count)
    try:
        yield seen
    finally:
        sa.event.remove(engine, "before_cursor_execute", count)


@pytest.mark.req("ENF-PERF-007")
def test_batch_gives_what_the_single_read_gives() -> None:
    with lib_env() as lib:
        units = [lib.new_unit()[0] for _ in range(6)]
        with lib.engine.begin() as connection:
            for n, unit in enumerate(units[:5]):  # the last Unit has no copy
                for k in range(1 + n % 3):  # one to three copies, in replica order
                    put_copy(connection, lib, unit, f"{n}-{k}.cbz")
        with lib.engine.connect() as connection:
            batch = library_copies_of(connection, [*units, units[0]])  # a repeated id
            for unit in units:
                assert batch.get(unit, []) == list(library_copies(connection, unit))
            assert units[5] not in batch  # no copy: no key, not an empty list
            assert [len(batch[u]) for u in units[:5]] == [1, 2, 3, 1, 2]


@pytest.mark.req("ENF-PERF-007")
def test_batch_is_a_few_statements_however_many_units() -> None:
    with lib_env() as lib, lib.engine.connect() as connection:
        ids = [new_ulid() for _ in range(1_200)]  # more than one chunk, none has a copy
        with statements(lib.engine) as seen:
            assert library_copies_of(connection, ids) == {}
        assert len(seen) == 3  # 500 + 500 + 200, not 1 200


@pytest.mark.req("ENF-PERF-007")
def test_export_never_takes_a_damaged_copy_as_its_source() -> None:
    with lib_env() as lib:
        unit, unit_variant = lib.new_unit()
        other, other_variant = lib.new_unit()
        with lib.engine.begin() as connection:
            for u, v in ((unit, unit_variant), (other, other_variant)):
                connection.execute(
                    sa.insert(storage.content).values(
                        id=new_ulid(), unit_id=u, variant_id=v, kind="images", format="cbz",
                        media_type="application/vnd.comicbook+zip", retention="keep",
                        original_sha256=sha(u), path=f"manga/{u}.cbz",
                        store_provenance="received", size=10,
                        content_hash=sha(u), watermarked=False, page_count=2,
                    )
                )  # fmt: skip
            bad = put_copy(connection, lib, unit, "bad.cbz")
            put_copy(connection, lib, unit, "good.cbz")
            damage(connection, bad)
            only_bad = put_copy(connection, lib, other, "only-bad.cbz")
            damage(connection, only_bad)
        with lib.engine.connect() as connection:
            found = export_sources(connection, [unit, other, new_ulid()])
        assert (found[unit].local_path or "").endswith(
            "/good.cbz"
        )  # the healthy one, not the first
        assert found[other].local_path is None  # damaged only: nothing to read
        assert len(found) == 2  # a Unit with no received archive is not an answer
