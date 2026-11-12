"""EF-BIB-012 end to end: the reacquisition goes through the real ticket path and the real store.

A chapter is received by the follow cycle, its archive is damaged, the check marks it and asks for
the Unit again, the ticket fetches it and files it as a repair copy beside the damaged file (the
Variant already has its archive: this is what used to be swallowed and thrown away), and the Unit
is `ok` once the fresh copy has been read.
"""

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.reacquire import ReacquireOutcome, Reacquirer
from shelfhold.export.application.library_repair import LibraryRepair
from shelfhold.export.domain.physical_roots import RECYCLE_DIR
from shelfhold.infrastructure.schema import platform
from shelfhold.library.application.integrity import IntegrityChecker, check_cbz
from shelfhold.library.public import irreplaceability
from shelfhold.library.replica import independent_copies
from tests.follow_env import follow_bench
from tests.integrity_env import flip_page_byte
from tests.locks_env import work_lock

pytestmark = pytest.mark.req("EF-BIB-012")


def test_reacquisition_files_a_repair_copy_through_the_ticket_path() -> None:
    with follow_bench() as bench:
        bench.run()
        bench.add_chapter(3)
        for _ in range(4):
            bench.run()
        (unit_id,) = [
            r["unit_id"]
            for r in bench.rows("SELECT unit_id FROM download_request WHERE state = 'fetched'")
        ]
        (damaged,) = [p for p in bench.library_files() if p.suffix == ".cbz"][
            -1:
        ]  # the chapter that was fetched
        engine = bench.lib.engine
        with engine.begin() as connection:  # the intent journal needs the instance row
            connection.execute(
                sa.insert(platform.instance).values(
                    instance_id=bench.lib.instance_id, jurisdiction="FR"
                )
            )
        bench.cycle = bench.new_cycle(  # the repair the runtime wires into the ticket
            repairer=LibraryRepair(
                engine,
                bench.lib.fsops,
                bench.lib.locate,
                bench.clock,
                hold_work=work_lock(engine),
                bus=bench.bus,
            )
        )
        good = damaged.read_bytes()
        bench.clock.advance(hours=1)  # the check is later than the reception
        flip_page_byte(damaged)
        before = damaged.read_bytes()
        checker = IntegrityChecker(engine, bench.bus, bench.clock)
        reacquirer = Reacquirer(
            engine, bench.bus, bench.clock,
            irreplaceable=lambda c, u: irreplaceability(c, u).irreplaceable,
        )  # fmt: skip

        with engine.connect() as connection:
            held = independent_copies(connection, unit_id)  # the library file and the origin
        assert checker.check_unit(unit_id).state == "corrupt"
        with engine.connect() as connection:
            assert independent_copies(connection, unit_id) == held - 1
        assert reacquirer.reacquire(unit_id) is ReacquireOutcome.REQUESTED
        bench.clock.advance(minutes=5)  # the fetch is later than the check
        for _ in range(4):
            bench.run()
        newest = bench.rows(
            "SELECT state FROM download_request WHERE unit_id = :u ORDER BY requested_at DESC",
            u=unit_id,
        )[0]
        assert newest["state"] == "fetched"

        files = [p for p in bench.library_files() if p.suffix == ".cbz"]
        assert files == [
            damaged
        ]  # no duplicate chapter, no `repair-*` file in the tree Kavita reads
        assert damaged.read_bytes() == good and before != good  # the served path is whole again
        assert check_cbz(damaged, expected_pages=None, expected_hash=None).ok
        binned = [p for p in (bench.lib.library / RECYCLE_DIR).rglob("*") if p.is_file()]
        assert [p.read_bytes() for p in binned] == [before]  # the damaged file is in the bin
        assert len(bench.rows("SELECT id FROM content WHERE unit_id = :u", u=unit_id)) == 1
        bench.clock.advance(hours=1)
        done = reacquirer.complete(unit_id, repaired=checker.repaired)
        assert done.outcome == "replaced"
        with engine.connect() as connection:
            assert independent_copies(connection, unit_id) == held
            state = connection.execute(
                sa.text("SELECT integrity_state FROM unit WHERE id = :u"), {"u": unit_id}
            ).scalar_one()
        assert state == "ok"
