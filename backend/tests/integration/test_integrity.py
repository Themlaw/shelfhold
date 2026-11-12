"""EF-BIB-012: the integrity check, the reacquisition of the whole Unit, the KOReader reset.

A damaged archive is marked and asked for again in full; the old file stays where it is until the
new one is verified, then the same-path upgrade replaces it (and says KOReader starts again). An
irreplaceable Unit has nothing to be asked of: it is flagged and left alone, never replaced.
"""

from datetime import timedelta

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.reacquire import ReacquireOutcome, Replacement
from shelfhold.events.activity import ActivityEvent, Viewer, activity_view
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.ops.status import koreader_reset_report
from tests.export_env import export_env
from tests.integrity_env import Rig, flip_page_byte
from tests.upgrade_env import better_copy, bin_files, export_one, make_irreplaceable

pytestmark = pytest.mark.req("EF-BIB-012")


def _rig(env) -> Rig:  # type: ignore[no-untyped-def]
    return Rig(env, export_one(env))


def _damage(rig: Rig, how: str) -> str:
    if how == "crc":
        flip_page_byte(rig.received())
    else:
        with rig.env.engine.begin() as connection:  # a page is missing: two were recorded
            connection.execute(sa.text("UPDATE content SET page_count = 2"))
    return how if how == "crc" else "page_count"


def _complete(rig: Rig, content_id_hint: str | None = None) -> Replacement:
    checker = rig.checker()
    return rig.reacquirer().complete(
        rig.exported.unit_id,
        verify=checker.verify_content,
        upgrade=lambda unit, content: [
            r.outcome.value
            for r in rig.upgrader().upgrade_unit(unit, content, reason="integrity_reacquire")  # type: ignore[attr-defined]
        ],
    )


@pytest.mark.parametrize("how", ["crc", "pages"])
def test_integrity_crc_and_page_count_reacquires_whole_unit(how: str) -> None:
    with export_env() as env:
        rig = _rig(env)
        reason = _damage(rig, how)
        before_served = rig.exported.target.read_bytes()
        before_received = rig.received().read_bytes()

        verdict = rig.checker().check_unit(rig.exported.unit_id)
        assert (verdict.state, verdict.reason) == ("corrupt", reason)
        assert rig.state()[0] == "corrupt"

        assert rig.reacquirer().reacquire(rig.exported.unit_id) is ReacquireOutcome.REQUESTED
        (request,) = rig.requests()
        assert (request.target_kind, request.state) == ("unit", "requested")
        # the whole Unit is asked for again; nothing is replaced or deleted yet
        assert rig.exported.target.read_bytes() == before_served
        assert rig.received().read_bytes() == before_received

        better_copy(env, rig.exported)  # the file arrives (a new content of the same Unit)
        done = _complete(rig)
        assert done.outcome == "replaced"
        assert rig.exported.target.read_bytes() != before_served  # same path, new file
        assert len(bin_files(env)) == 1  # the old version is in the bin, not gone
        assert rig.state()[0] == "ok"


def test_integrity_reacquire_logs_koreader_reset() -> None:
    with export_env() as env:
        rig = _rig(env)
        _damage(rig, "crc")
        rig.checker().check_unit(rig.exported.unit_id)
        rig.reacquirer().reacquire(rig.exported.unit_id)
        better_copy(env, rig.exported)
        assert _complete(rig).outcome == "replaced"

        (event,) = rig.events("unit.upgraded")
        assert event.payload["reason"] == "integrity_reacquire"
        assert event.payload["koreader_reset"] is True
        assert event.visibility == "ops"  # the Activity of the admin, nobody else's
        with env.engine.connect() as connection:
            listed = koreader_reset_report(connection, env.clock.now() - timedelta(days=1))
        assert [(r["unit_id"], r["reason"]) for r in listed] == [
            (rig.exported.unit_id, "integrity_reacquire")
        ]


def test_corrupt_irreplaceable_unit_alerts_never_deleted() -> None:
    with export_env() as env:
        rig = _rig(env)
        _damage(rig, "crc")
        better_copy(env, rig.exported)  # a good copy exists, as a file...
        make_irreplaceable(env)  # ...but no live source lists the Unit any more
        received, served = rig.received(), rig.exported.target
        before = (received.read_bytes(), served.read_bytes())

        assert rig.checker().check_unit(rig.exported.unit_id).state == "corrupt"
        assert rig.reacquirer().reacquire(rig.exported.unit_id) is ReacquireOutcome.IRREPLACEABLE
        assert len(rig.events("ops.integrity_irreplaceable")) == 1  # the alert
        assert rig.requests() == []  # nothing is asked of anyone
        # even a good copy that is there is not swapped in on its own
        assert _complete(rig).outcome == "irreplaceable"
        assert (received.read_bytes(), served.read_bytes()) == before
        assert bin_files(env) == []
        assert rig.state()[0] == "corrupt"
        with env.engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM content")).scalar_one() == 2


def test_original_stays_until_the_new_copy_is_verified() -> None:
    with export_env() as env:
        rig = _rig(env)
        _damage(rig, "crc")
        rig.checker().check_unit(rig.exported.unit_id)
        rig.reacquirer().reacquire(rig.exported.unit_id)
        received, served = rig.received(), rig.exported.target
        before = (received.read_bytes(), served.read_bytes())

        bad = better_copy(env, rig.exported)
        with env.engine.connect() as connection:
            variant = connection.execute(
                sa.text("SELECT variant_id FROM content WHERE id = :c"), {"c": bad}
            ).scalar_one()
        with env.engine.begin() as connection:  # the new copy is damaged too
            connection.execute(
                sa.text("UPDATE content SET page_count = 9 WHERE id = :c"), {"c": bad}
            )
        assert variant
        done = _complete(rig)
        assert done.outcome == "fresh_copy_corrupt"
        assert (received.read_bytes(), served.read_bytes()) == before
        assert received.exists() and bin_files(env) == []
        assert rig.state()[0] == "corrupt"


def test_a_healthy_unit_is_marked_ok_and_not_asked_again() -> None:
    with export_env() as env:
        rig = _rig(env)
        verdict = rig.checker().check_unit(rig.exported.unit_id)
        assert (verdict.state, verdict.reason) == ("ok", None)
        assert rig.state()[0] == "ok" and rig.state()[1] is not None
        assert rig.reacquirer().reacquire(rig.exported.unit_id) is ReacquireOutcome.NOT_CORRUPT
        assert rig.requests() == []


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
    """IDOR: replacing the file records a reset notice for the admin; it deletes no one's
    position, and the notice is not shown to a member or a child."""
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
        _damage(rig, "crc")
        rig.checker().check_unit(rig.exported.unit_id)
        rig.reacquirer().reacquire(rig.exported.unit_id)
        better_copy(env, rig.exported)
        assert _complete(rig).outcome == "replaced"

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
