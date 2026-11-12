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
from shelfhold.library.application.integrity import check_cbz
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
    return rig.reacquirer().complete(
        rig.exported.unit_id, repaired=repaired or rig.checker().repaired
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

        # the fresh bytes arrive: they take the damaged file's place, which goes to the bin
        rig.env.clock.advance(days=1)  # the check is older than the repair
        assert rig.repair_copy(good) is True
        assert damaged.read_bytes() == good and before != good  # the served path is whole
        assert [p.read_bytes() for p in bin_files(env)] == [before]  # the old one is kept
        assert not [p for p in env.lib.files_under(env.lib.library) if "repair-" in p.name]
        assert check_cbz(damaged, expected_pages=1, expected_hash=None).ok
        done = _complete(rig)
        assert done.outcome == "replaced", done
        assert rig.state()[0] == "ok" and _copies(rig) == 1
        (event,) = rig.events("unit.upgraded")  # the Activity says KOReader starts again
        assert event.payload["koreader_reset"] is True
        assert event.payload["reason"] == "integrity_reacquire" and event.visibility == "ops"
        with env.engine.connect() as connection:
            listed = koreader_reset_report(connection, env.clock.now() - timedelta(days=2))
        assert [(r["unit_id"], r["reason"]) for r in listed] == [
            (rig.exported.unit_id, "integrity_reacquire")
        ]


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
        assert rig.repair_copy(good) is False  # even good bytes are not swapped in on their own
        assert _complete(rig).outcome == "irreplaceable"
        assert (received.read_bytes(), served.read_bytes()) == before
        assert bin_files(env) == [] and rig.state()[0] == "corrupt"


def test_a_repair_that_is_not_the_recorded_payload_is_refused() -> None:
    with export_env() as env:
        rig = _rig(env)
        _damage(rig, "crc")
        rig.checker().check_unit(rig.exported.unit_id)
        before = rig.received().read_bytes()
        assert rig.repair_copy(cbz_bytes((("1-001.png", PNG + b"another page"),))) is False
        assert rig.received().read_bytes() == before and bin_files(env) == []
        assert rig.state()[0] == "corrupt" and _copies(rig) == 0


def _keep_in_store(rig: Rig, good: bytes):  # type: ignore[no-untyped-def]
    """A kept original in /store, independent and verified, as the safeguard leaves it."""
    env = rig.env
    kept = env.lib.store / "kept.cbz"
    kept.write_bytes(good)
    with env.engine.begin() as connection:
        row = connection.execute(sa.text("SELECT content_hash, original_sha256 FROM content")).one()
        record_replica(
            connection, unit_id=rig.exported.unit_id, location="store",
            content_hash=row[0], file_hash=row[1], independent=True,
            physical_root_id=env.lib.store_root, rel_path="kept.cbz", size=len(good),
            verified_at=env.clock.now(),
        )  # fmt: skip
    return kept


def _damaged_at(rig: Rig, location: str):  # type: ignore[no-untyped-def]
    with rig.env.engine.connect() as connection:
        return connection.execute(
            sa.text("SELECT damaged_at FROM replica WHERE unit_id = :u AND location = :l"),
            {"u": rig.exported.unit_id, "l": location},
        ).scalar_one()


def test_an_intact_store_copy_is_read_and_the_unit_returns_ok_after_a_repair() -> None:
    with export_env() as env:
        rig = _rig(env)
        good = rig.received().read_bytes()
        kept = _keep_in_store(rig, good)
        flip_page_byte(rig.received())
        assert rig.checker().check_unit(rig.exported.unit_id).state == "corrupt"
        assert _damaged_at(rig, "store") is None  # the intact kept original is not marked
        env.clock.advance(days=1)
        assert rig.repair_copy(good) is True
        assert _complete(rig).outcome == "replaced"
        assert rig.state()[0] == "ok" and kept.read_bytes() == good


def test_a_corrupt_store_copy_is_detected() -> None:
    with export_env() as env:
        rig = _rig(env)
        kept = _keep_in_store(rig, rig.received().read_bytes())
        flip_page_byte(kept)
        verdict = rig.checker().check_unit(rig.exported.unit_id)
        assert (verdict.state, verdict.reason) == ("corrupt", "crc")
        assert _damaged_at(rig, "store") is not None and kept.exists()


def test_the_unit_is_not_ok_while_a_place_holds_only_damaged_copies() -> None:
    """A damaged copy in /store is not made good by a repaired one in /library."""
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
                verified_at=env.clock.now(),
            )  # fmt: skip
        flip_page_byte(kept)
        flip_page_byte(rig.received())
        assert rig.checker().check_unit(rig.exported.unit_id).state == "corrupt"
        env.clock.advance(days=1)
        assert rig.repair_copy(good) is True
        done = _complete(rig)
        assert done.outcome == "fresh_copy_corrupt"
        assert rig.state()[0] == "corrupt" and kept.exists()
        assert _damaged_at(rig, "store") is not None  # it was read, found damaged and marked


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


def test_handing_a_ticket_over_never_takes_a_purges_leftover() -> None:
    """The service names a chapter's download by one token: it follows the live request, but a
    request a purge closed keeps its own (`leftover_tickets` still lists it), and so does one that
    is still open."""
    from types import SimpleNamespace

    from shelfhold.acquisition.application.tickets import DownloadTickets
    from shelfhold.library.public import leftover_tickets

    with export_env() as env:
        rig = _rig(env)
        binding = rig.exported.series.binding_id
        with env.engine.connect() as connection:
            variant = connection.execute(
                sa.text("SELECT id FROM unit_variant LIMIT 1")
            ).scalar_one()
        ids = {name: new_ulid() for name in ("purged", "fetched", "open", "new")}
        with env.engine.begin() as connection:
            for name, state, token in (
                ("purged", "closed_by_purge", "t-purged"),
                ("fetched", "fetched", "t-fetched"),
                ("open", "requested", "t-open"),
            ):
                connection.execute(
                    sa.text(
                        "INSERT INTO download_request (id, target_kind, variant_id, unit_id, "
                        "source_binding_id, ticket, state, requested_at) VALUES "
                        "(:i, 'unit', :v, :u, :b, :t, :s, :now)"
                    ),
                    {"i": ids[name], "v": None if name == "purged" else variant,
                     "u": None if name != "open" else rig.exported.unit_id, "b": binding,
                     "t": token, "s": state, "now": env.clock.now()},
                )  # fmt: skip
        tickets = object.__new__(DownloadTickets)
        tickets._engine = env.engine  # type: ignore[attr-defined]
        for token in ("t-purged", "t-fetched", "t-open"):
            tickets._hand_over(SimpleNamespace(id=ids["new"], binding_id=binding), token)  # type: ignore[attr-defined,arg-type]
        with env.engine.connect() as connection:
            held = dict(
                connection.execute(sa.text("SELECT id, ticket FROM download_request")).all()
            )
            assert [t.ticket for t in leftover_tickets(connection)] == ["t-purged"]
        assert held[ids["purged"]] == "t-purged" and held[ids["open"]] == "t-open"
        assert held[ids["fetched"]] is None  # only a finished fetch lets go of the token


def test_when_every_copy_is_damaged_nothing_proves_a_copy_and_the_unit_is_irreplaceable() -> None:
    """No purge upstream can rest on a damaged copy: it counts for nothing, and a Unit with no
    live source left is irreplaceable."""
    from shelfhold.library.public import irreplaceability

    with export_env() as env:
        rig = _rig(env)
        flip_page_byte(rig.received())
        make_irreplaceable(env)
        assert rig.checker().check_unit(rig.exported.unit_id).state == "corrupt"
        with env.engine.connect() as connection:
            verdict = irreplaceability(connection, rig.exported.unit_id)
        assert verdict.irreplaceable is True and verdict.independent_copies == 0
        assert rig.reacquirer().reacquire(rig.exported.unit_id) is ReacquireOutcome.IRREPLACEABLE
        assert len(rig.events("ops.integrity_irreplaceable")) == 1
