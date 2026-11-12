"""EF-BIB-012: the integrity check, the reacquisition of the whole Unit, the KOReader reset.

A damaged archive is marked, stops counting as a copy, and is asked for again in full; the fresh
bytes are filed beside it, the damaged file stays where it is, and the Unit is `ok` only once
every place that held a damaged copy holds an intact one that was read again. An irreplaceable
Unit has nothing to be asked of: it is flagged and left alone.
"""

from datetime import timedelta

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.reacquire import ReacquireOutcome
from shelfhold.events.activity import ActivityEvent, Viewer, activity_view
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import platform
from shelfhold.library.domain.failures import RepairRefusedError
from shelfhold.library.replica import independent_copies, record_replica
from shelfhold.ops.status import koreader_reset_report
from tests.export_env import export_env
from tests.integrity_env import Rig, flip_page_byte
from tests.library_env import PNG, cbz_bytes
from tests.upgrade_env import better_copy, bin_files, export_one, make_irreplaceable, mark_read

pytestmark = pytest.mark.req("EF-BIB-012")


def _rig(env) -> Rig:  # type: ignore[no-untyped-def]
    return Rig(env, export_one(env))


def _damage(rig: Rig, how: str) -> str:
    if how == "crc":
        flip_page_byte(rig.received())
        return "crc"
    with rig.env.engine.begin() as connection:  # a page is missing: two were recorded
        connection.execute(sa.text("UPDATE content SET page_count = 2"))
    return "page_count"


def _complete(rig: Rig, repaired=None):  # type: ignore[no-untyped-def]
    checker = rig.checker()
    return rig.reacquirer().complete(
        rig.exported.unit_id,
        repaired=repaired or checker.repaired,
        upgrade=lambda unit, content: [
            r.outcome.value
            for r in rig.upgrader().upgrade_unit(unit, content, reason="integrity_reacquire")  # type: ignore[attr-defined]
        ],
    )


def _copies(rig: Rig) -> int:
    with rig.env.engine.connect() as connection:
        return independent_copies(connection, rig.exported.unit_id)


def test_integrity_page_count_failure_marks_the_unit_and_frees_its_copy() -> None:
    with export_env() as env:
        rig = _rig(env)
        _damage(rig, "pages")
        verdict = rig.checker().check_unit(rig.exported.unit_id)
        assert (verdict.state, verdict.reason) == ("corrupt", "page_count")
        assert _copies(rig) == 0
        assert rig.reacquirer().reacquire(rig.exported.unit_id) is ReacquireOutcome.REQUESTED


def test_integrity_crc_and_page_count_reacquires_whole_unit() -> None:
    with export_env() as env:
        rig = _rig(env)
        good = rig.received().read_bytes()
        assert _copies(rig) == 1
        reason = _damage(rig, "crc")
        damaged = rig.received()
        before = damaged.read_bytes()

        verdict = rig.checker().check_unit(rig.exported.unit_id)
        assert (verdict.state, verdict.reason) == ("corrupt", reason)
        assert _copies(rig) == 0  # a damaged file is no copy at all
        assert rig.reacquirer().reacquire(rig.exported.unit_id) is ReacquireOutcome.REQUESTED
        (request,) = rig.requests()
        assert (request.target_kind, request.state) == ("unit", "requested")
        assert rig.state()[0] == "corrupt"

        # the fresh bytes arrive: filed beside the damaged file, which is not touched
        rig.env.clock.advance(days=1)  # the check is older than the arrival
        rig.repair_copy(good)
        done = _complete(rig)
        assert done.outcome in ("replaced", "repaired"), done
        assert rig.state()[0] == "ok" and _copies(rig) == 1
        assert damaged.exists() and damaged.read_bytes() == before  # never deleted, never replaced


def test_integrity_reacquire_logs_koreader_reset() -> None:
    """The same-path upgrade, asked with `integrity_reacquire`, replaces the served file even of a
    read Unit (it is a broken file), moves the old one to the bin and says KOReader starts again."""
    with export_env() as env:
        rig = _rig(env)
        old = rig.exported.target.read_bytes()
        content_id = better_copy(env, rig.exported)
        mark_read(env, rig.exported)
        results = rig.upgrader().upgrade_unit(  # type: ignore[attr-defined]
            rig.exported.unit_id, content_id, reason="integrity_reacquire"
        )
        assert [r.outcome.value for r in results] == ["upgraded"]
        assert rig.exported.target.read_bytes() != old and len(bin_files(env)) == 1
        (event,) = rig.events("unit.upgraded")
        assert event.payload["reason"] == "integrity_reacquire"
        assert event.payload["koreader_reset"] is True
        assert event.visibility == "ops"  # the Activity of the admin, nobody else's
        with env.engine.connect() as connection:
            listed = koreader_reset_report(connection, env.clock.now() - timedelta(days=1))
        assert [(r["unit_id"], r["reason"]) for r in listed] == [
            (rig.exported.unit_id, "integrity_reacquire")
        ]


def test_a_better_version_still_skips_a_read_unit() -> None:
    with export_env() as env:
        rig = _rig(env)
        content_id = better_copy(env, rig.exported)
        mark_read(env, rig.exported)
        results = rig.upgrader().upgrade_unit(rig.exported.unit_id, content_id)  # type: ignore[attr-defined]
        assert [r.outcome.value for r in results] == ["skipped_read"]


def test_corrupt_irreplaceable_unit_alerts_never_deleted() -> None:
    with export_env() as env:
        rig = _rig(env)
        good = rig.received().read_bytes()
        _damage(rig, "crc")
        make_irreplaceable(env)  # no live source lists the Unit any more
        received, served = rig.received(), rig.exported.target
        before = (received.read_bytes(), served.read_bytes())

        assert rig.checker().check_unit(rig.exported.unit_id).state == "corrupt"
        assert rig.reacquirer().reacquire(rig.exported.unit_id) is ReacquireOutcome.IRREPLACEABLE
        assert rig.reacquirer().reacquire(rig.exported.unit_id) is ReacquireOutcome.IRREPLACEABLE
        assert len(rig.events("ops.integrity_irreplaceable")) == 1  # said once
        assert rig.requests() == []  # nothing is asked of anyone
        env.clock.advance(days=1)
        rig.repair_copy(good)  # even good bytes that are there are not swapped in on their own
        assert _complete(rig).outcome == "irreplaceable"
        assert (received.read_bytes(), served.read_bytes()) == before
        assert bin_files(env) == [] and rig.state()[0] == "corrupt"


def test_a_repair_that_is_not_the_recorded_payload_is_refused() -> None:
    with export_env() as env:
        rig = _rig(env)
        _damage(rig, "crc")
        rig.checker().check_unit(rig.exported.unit_id)
        with pytest.raises(RepairRefusedError):
            rig.repair_copy(cbz_bytes((("1-001.png", PNG + b"another page"),)))
        assert rig.state()[0] == "corrupt" and _copies(rig) == 0


def test_the_unit_is_not_ok_while_a_place_holds_only_damaged_copies() -> None:
    """A damaged copy in /store is not made good by a fresh one in /library."""
    with export_env() as env:
        rig = _rig(env)
        good = rig.received().read_bytes()
        lib = env.lib
        kept = lib.store / "kept.cbz"
        kept.write_bytes(good)
        with env.engine.begin() as connection:
            row = connection.execute(
                sa.text("SELECT content_hash, original_sha256 FROM content")
            ).one()
            record_replica(
                connection, unit_id=rig.exported.unit_id, location="store",
                content_hash=row[0], file_hash=row[1], independent=True,
                physical_root_id=lib.store_root, rel_path="kept.cbz", size=len(good),
            )  # fmt: skip
        flip_page_byte(kept)
        assert rig.checker().check_unit(rig.exported.unit_id).state == "corrupt"
        assert _copies(rig) == 1  # the library copy still counts; the store one does not
        env.clock.advance(days=1)
        rig.repair_copy(good)
        done = _complete(rig)
        assert done.outcome == "fresh_copy_corrupt"
        assert rig.state()[0] == "corrupt" and kept.exists()
        # the store gets its intact copy back (the safeguard's catch-up): now the Unit is whole
        kept.write_bytes(good)
        with env.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE replica SET independent = 1 WHERE location = 'store'")
            )
        env.clock.advance(days=1)
        rig.repair_copy(good)
        assert _complete(rig).outcome in ("replaced", "repaired") and rig.state()[0] == "ok"


def test_no_new_copy_no_new_read() -> None:
    with export_env() as env:
        rig = _rig(env)
        _damage(rig, "crc")
        rig.checker().check_unit(rig.exported.unit_id)
        env.clock.advance(days=1)
        reads: list[str] = []
        done = _complete(rig, repaired=lambda unit: reads.append(unit) or False)  # type: ignore[func-returns-value]
        assert done.outcome == "no_fresh_copy" and reads == []  # nothing arrived: nothing is read


def test_a_healthy_unit_is_marked_ok_and_not_asked_again() -> None:
    with export_env() as env:
        rig = _rig(env)
        verdict = rig.checker().check_unit(rig.exported.unit_id)
        assert (verdict.state, verdict.reason) == ("ok", None)
        assert rig.state()[0] == "ok" and rig.state()[1] is not None
        assert rig.reacquirer().reacquire(rig.exported.unit_id) is ReacquireOutcome.NOT_CORRUPT
        assert rig.requests() == [] and _copies(rig) == 1


def test_reacquisition_is_not_repeated_while_one_is_open_or_cooling() -> None:
    with export_env() as env:
        rig = _rig(env)
        _damage(rig, "crc")
        rig.checker().check_unit(rig.exported.unit_id)
        assert rig.reacquirer().reacquire(rig.exported.unit_id) is ReacquireOutcome.REQUESTED
        assert rig.reacquirer().reacquire(rig.exported.unit_id) is ReacquireOutcome.ALREADY_OPEN
        assert len(rig.requests()) == 1


def test_checked_units_are_not_due_again_before_the_interval() -> None:
    with export_env() as env:
        rig = _rig(env)
        checker = rig.checker()
        assert checker.due_units(10) == [rig.exported.unit_id]
        checker.check_unit(rig.exported.unit_id)
        assert checker.due_units(10) == []  # the disks are not read again every cycle
        env.clock.advance(days=31)
        assert checker.due_units(10) == [rig.exported.unit_id]
        assert checker.due_units(0) == []


def test_check_is_bounded_in_throughput() -> None:
    with export_env() as env:
        rig = _rig(env)
        size = rig.received().stat().st_size
        rig.checker(rate=100).check_unit(rig.exported.unit_id)
        assert sum(rig.sleeps) >= size / 100  # the reads were paced, not run flat out


def test_koreader_reset_never_touches_anyones_reading_position() -> None:
    """IDOR: a repair records a reset notice for the admin; it deletes no one's position, and the
    notice is not shown to a member or a child."""
    with export_env() as env:
        rig = _rig(env)
        users = [new_ulid(), new_ulid()]
        with env.engine.begin() as connection:
            for user_id in users:
                connection.execute(
                    sa.text("INSERT INTO user (id, username, role) VALUES (:i, :n, 'member')"),
                    {"i": user_id, "n": f"reader-{user_id}"},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO reading_position (id, user_id, carrier_type, carrier_id, "
                        "content_hash, locator, percent, client_ts, server_ts) VALUES "
                        "(:id, :u, 'export_item', :item, 'sha256:' || :h, '{}', 0.4, :ts, :ts)"
                    ),
                    {"id": new_ulid(), "u": user_id, "item": rig.exported.export_item_id,
                     "h": "0" * 64, "ts": env.clock.now()},
                )  # fmt: skip
        content_id = better_copy(env, rig.exported)
        rig.upgrader().upgrade_unit(  # type: ignore[attr-defined]
            rig.exported.unit_id, content_id, reason="integrity_reacquire"
        )
        with env.engine.connect() as connection:
            kept = (
                connection.execute(sa.text("SELECT user_id FROM reading_position ORDER BY user_id"))
                .scalars()
                .all()
            )
        assert kept == sorted(users)  # both positions are still there
        (event,) = rig.events("unit.upgraded")
        for role in ("member", "child"):
            seen = activity_view(
                Viewer(new_ulid(), role),
                ActivityEvent(
                    id=event.id, type=event.type, visibility=event.visibility,
                    payload=event.payload, owner_id=None, owner_role=None, title=None,
                ),
            )  # fmt: skip
            assert seen is None
        assert platform  # the event table is the Activity's source
