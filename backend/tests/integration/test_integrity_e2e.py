"""EF-BIB-012 end to end: the reacquisition goes through the real ticket path and the real store.

A chapter is received by the follow cycle, its archive is damaged, the check marks it and asks for
the Unit again, the ticket fetches it and files it as a repair copy beside the damaged file (the
Variant already has its archive: this is what used to be swallowed and thrown away), and the Unit
is `ok` once the fresh copy has been read.
"""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.reacquire import ReacquireOutcome, Reacquirer
from shelfhold.library.application.integrity import IntegrityChecker
from shelfhold.library.public import irreplaceability
from shelfhold.library.replica import independent_copies
from tests.follow_env import follow_bench
from tests.integrity_env import flip_page_byte

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
        good = damaged.read_bytes()
        bench.clock.set(datetime.now(UTC))  # the check is later than the reception
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
        for _ in range(4):
            bench.run()
        newest = bench.rows(
            "SELECT state FROM download_request WHERE unit_id = :u ORDER BY requested_at DESC",
            u=unit_id,
        )[0]
        assert newest["state"] == "fetched"

        files = [p for p in bench.library_files() if p.suffix == ".cbz"]
        assert damaged in files and len(files) == 2  # the damaged one stays, a fresh one beside
        assert damaged.read_bytes() == before and before != good
        assert len(bench.rows("SELECT id FROM content WHERE unit_id = :u", u=unit_id)) == 1
        done = reacquirer.complete(unit_id, repaired=checker.repaired, upgrade=lambda *_: [])
        assert done.outcome == "repaired"
        with engine.connect() as connection:
            assert independent_copies(connection, unit_id) == held
            state = connection.execute(
                sa.text("SELECT integrity_state FROM unit WHERE id = :u"), {"u": unit_id}
            ).scalar_one()
        assert state == "ok"
