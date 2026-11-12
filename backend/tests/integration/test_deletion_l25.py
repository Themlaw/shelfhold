"""EF-BIB-021, EF-BIB-031 (L25): deletion at three levels, with the impact shown first, the bin of
30 days, the empty managed folders removed after the scan, the typed word for an irreplaceable
Unit. Real files, real SQLite, real pipeline; every "never" below is a test that tries to break it.
"""

import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.export.application.deletion import (
    DeletionBlockedError,
    DeletionService,
    DeletionUnknownError,
    RestoreBlockedError,
)
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import LockBusyError, ResourceLocks, work_resource
from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.recycle_sweep import RecycleSweeper
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.library.public import (
    ConfirmationRequiredError,
    DeletionLevel,
    DeletionRefusedError,
)
from tests.deletion_env import (
    Deletion,
    file_hash,
    make_deletion,
    make_irreplaceable,
    open_intents,
    spread_unit,
)
from tests.export_env import ExportEnv, export_env
from tests.fs_spy import FsSpy
from tests.purge_env import ADMIN, KEY, STALE

UNFOLLOW = DeletionLevel.UNFOLLOW
LEVEL2 = DeletionLevel.REMOVE_DESTINATIONS
LEVEL3 = DeletionLevel.DELETE_CONTENT

MEMBER = Principal(
    "01J00000000000000000000MEM", "member", "member", Credential.SESSION,
    session_id="01J0000000000000000000SES1", auth_time=datetime(2026, 11, 5, 8, 0, tzinfo=UTC),
)  # fmt: skip
CHILD = Principal(
    "01J00000000000000000000CHD", "child", "child", Credential.SESSION,
    session_id="01J0000000000000000000SES2", auth_time=datetime(2026, 11, 5, 8, 0, tzinfo=UTC),
)  # fmt: skip
WRITERS = {"mkdir", "unlink", "remove", "rmdir", "rename", "replace", "utime", "symlink", "link",
           "truncate", "chmod", "renameat2_noreplace"}  # fmt: skip


def dest_file(env: ExportEnv, unit: str) -> Path:
    [path] = sorted((env.lib.manga / "Kavita").rglob("*.cbz"))
    return path


def replicas(d: Deletion, unit: str) -> list[tuple[object, ...]]:
    return d.rows(
        "SELECT id, location, rel_path, file_hash, dev, ino, independent FROM replica "
        "WHERE unit_id = :u ORDER BY id",
        u=unit,
    )


# -- the preview --------------------------------------------------------------------------------


@pytest.mark.req("EF-BIB-021")
def test_delete_three_levels_impact_preview() -> None:
    """Each level announces what it will do (exports, progress, files, irreplaceable Units) and
    does exactly that, and no more."""
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        before = d.tree()
        served = dest_file(env, unit)
        with env.engine.begin() as connection:  # a reader's position on the served file
            item = connection.execute(sa.select(export.export_item.c.id)).scalar_one()
            user = new_ulid()
            connection.execute(
                sa.text(
                    "INSERT INTO user (id, username, password_hash, role) "
                    "VALUES (:i, 'reader', NULL, 'member')"
                ),
                {"i": user},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO reading_position (id, user_id, carrier_type, carrier_id, "
                    "content_hash, locator, client_ts) VALUES (:i, :u, 'export_item', :c, "
                    "'sha256:' || hex(zeroblob(32)), '{}', CURRENT_TIMESTAMP)"
                ),
                {"i": new_ulid(), "u": user, "c": item},
            )

        one = d.service.preview(UNFOLLOW, [unit])
        two = d.service.preview(LEVEL2, [unit])
        three = d.service.preview(LEVEL3, [unit])

        assert (one.files, one.units[0].exports, one.units[0].progress) == (0, 0, 1)
        assert [f.place for f in two.units[0].files] == ["destination"]
        assert two.units[0].exports == 1 and two.units[0].progress == 1
        assert sorted(f.place for f in three.units[0].files) == ["destination", "library", "store"]
        assert not three.units[0].irreplaceable and three.units[0].independent_copies == 2
        assert three.units[0].independent_copies_after == 0
        assert len({one.version, two.version, three.version}) == 3

        d.apply(UNFOLLOW, [unit], plan=one)  # level 1: nothing moves
        assert d.tree() == before
        assert d.rows("SELECT follow_suppressed FROM unit WHERE id = :u", u=unit) == [(1,)]

        d.apply(LEVEL2, [unit])  # level 2: only the served file goes, to the bin
        after2 = d.tree()
        assert not served.exists()
        assert [k for k in before if k not in after2] == [
            f"library/{served.relative_to(env.lib.library)}"
        ]
        assert [r[1] for r in replicas(d, unit)] == ["library", "store"]  # /library and /store keep

        d.apply(LEVEL3, [unit])  # level 3: every managed copy, bin included
        assert replicas(d, unit) == []
        assert not any("received" in k or "units/" in k for k in d.tree() if "recycle" not in k)
        assert len(d.binned()) == 3


@pytest.mark.req("EF-BIB-021")
def test_preview_announces_exactly_what_the_run_does() -> None:
    """The preview compared with the effect: the files that left, their hashes, the bin entries,
    the folders removed and the counts are the ones it listed."""
    with export_env() as env:
        d = make_deletion(env)
        series = env.new_series("Dr Stone")
        units = [spread_unit(env, series, n) for n in ("1", "2")]
        before = d.tree()
        plan = d.service.preview(LEVEL3, units)
        listed = sorted(f.file_hash for u in plan.units for f in u.files)
        assert plan.files == len(listed) == 6 and plan.bytes > 0
        report = d.apply(LEVEL3, units, plan=plan)
        assert report.moved == plan.files  # type: ignore[attr-defined]
        after = d.tree()
        left = sorted(before[k] for k in before if k not in after)
        assert left == listed  # exactly the announced files left their places
        assert sorted(file_hash(p) for p in d.binned()) == listed  # and all of them are in a bin
        assert len(set(after) - set(before)) == 6  # the six bin entries, nothing else
        assert report.folders_removed == tuple(plan.folders)  # type: ignore[attr-defined]
        assert not (env.lib.manga / "Kavita" / "Dr Stone").exists()
        assert (env.lib.manga / "Kavita").is_dir()  # the destination itself stays


@pytest.mark.req("EF-BIB-021")
def test_preview_writes_nothing() -> None:
    """A spy on the file system and a dump of the database: the preview of every level changes
    neither, creates no lock and no intent."""
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        make_irreplaceable(env, unit)
        tree, dump = d.tree(), d.dump()  # the dump holds the intents and the locks too
        stamps = {p: p.stat().st_mtime_ns for p in env.lib.library.rglob("*")}
        with FsSpy([env.lib.library, env.lib.store]).watching() as spy:
            for level in DeletionLevel:
                d.service.preview(level, [unit])
        assert [a for a in spy.accesses if a.split(" ", 1)[0] in WRITERS] == []
        assert d.tree() == tree and d.dump() == dump
        assert {p: p.stat().st_mtime_ns for p in env.lib.library.rglob("*")} == stamps


@pytest.mark.req("EF-BIB-021")
def test_apply_refuses_a_preview_that_moved() -> None:
    """The version the admin read is the one applied: any change since (a file, the state of
    the Unit) is `plan.stale`, and nothing is written."""
    with export_env() as env:
        d = make_deletion(env)
        series = env.new_series("Dr Stone")
        unit = spread_unit(env, series)
        plan = d.service.preview(LEVEL3, [unit])
        make_irreplaceable(env, unit)  # became irreplaceable after it was read
        before = d.dump()
        with pytest.raises(StalePlanError):
            d.apply(LEVEL3, [unit], plan=plan, typed={unit: unit})
        assert d.dump() == before
        other = spread_unit(env, series, "2")
        with pytest.raises(StalePlanError):  # a preview of another Unit is not this one's
            d.apply(LEVEL3, [other], if_match=plan.version)
        with pytest.raises(DeletionUnknownError):
            d.service.preview(LEVEL3, ["01J000000000000000000UNKNO"])
        with pytest.raises(DeletionUnknownError):
            d.service.preview(LEVEL3, [])


# -- the irreplaceable Unit ----------------------------------------------------------------------


@pytest.mark.req("EF-BIB-021", "EF-BIB-031")
def test_irreplaceable_delete_requires_typed_confirmation() -> None:
    """Without a live source, the identifier must be typed: absent, wrong, another Unit's or a
    preview's own version typed instead are refused, and nothing moves."""
    with export_env() as env:
        d = make_deletion(env)
        series = env.new_series("Dr Stone")
        unit, other = spread_unit(env, series), spread_unit(env, series, "2")
        make_irreplaceable(env, unit)
        make_irreplaceable(env, other)
        plan = d.service.preview(LEVEL3, [unit])
        assert plan.units[0].irreplaceable and plan.units[0].needs_confirmation
        assert plan.confirmations == (unit,)
        tree, dump = d.tree(), d.dump()
        for typed in (None, {}, {unit: ""}, {unit: "yes"}, {unit: other}, {other: unit},
                      {unit: plan.version}, {unit: unit.lower()}):  # fmt: skip
            with pytest.raises(ConfirmationRequiredError):
                d.apply(LEVEL3, [unit], plan=plan, typed=typed)
            assert d.tree() == tree and d.dump() == dump
        # the confirmation of one preview does not serve another: two Units, one word typed
        both = d.service.preview(LEVEL3, [unit, other])
        with pytest.raises(ConfirmationRequiredError):
            d.apply(LEVEL3, [unit, other], plan=both, typed={unit: unit})
        assert d.tree() == tree
        report = d.apply(LEVEL3, [unit], plan=plan, typed={unit: unit})
        assert report.moved == 3  # type: ignore[attr-defined]
        assert replicas(d, unit) == [] and len(replicas(d, other)) == 3


@pytest.mark.req("EF-BIB-021", "EF-BIB-031")
def test_irreplaceability_is_read_fresh_from_the_registry() -> None:
    """A Unit with no live source is irreplaceable; one whose listed source is alive is not. The
    answer comes from the registry at the moment of the application, never from the preview."""
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        assert d.service.preview(LEVEL3, [unit]).units[0].needs_confirmation is False
        d.apply(LEVEL3, [unit])  # a live source: no word asked
        assert replicas(d, unit) == []
        # restore, then lose the source: the same Unit now asks for its word
        d.service.restore(unit, principal=ADMIN, now=d.now)
        make_irreplaceable(env, unit)
        fresh = d.service.preview(LEVEL3, [unit])
        assert fresh.units[0].irreplaceable and fresh.units[0].needs_confirmation
        with pytest.raises(ConfirmationRequiredError):
            d.apply(LEVEL3, [unit], plan=fresh)


@pytest.mark.req("EF-BIB-021", "EF-BIB-031")
def test_level_two_never_takes_a_store_original_or_the_last_copy() -> None:
    """Level 2 touches the destinations only: the /store original of an irreplaceable Unit stays
    in place, and a reading that would leave no independent copy is refused."""
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        make_irreplaceable(env, unit)
        store_copy = next(p for p in env.lib.store.rglob("*.cbz") if "recycle" not in p.parts)
        digest = file_hash(store_copy)
        d.apply(LEVEL2, [unit])  # the destination copy goes; /store and /library stay
        assert store_copy.exists() and file_hash(store_copy) == digest
        assert [r[1] for r in replicas(d, unit)] == ["library", "store"]
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        with env.engine.begin() as connection:  # the destination file is the only original left
            connection.execute(sa.text("DELETE FROM replica WHERE location IN ('library','store')"))
            connection.execute(sa.text("UPDATE replica SET independent = 1"))
        plan = d.service.preview(LEVEL2, [unit])
        assert plan.units[0].blocked == ("last_independent_copy",)
        assert plan.units[0].independent_copies == 1 and plan.units[0].independent_copies_after == 0
        tree, dump = d.tree(), d.dump()
        with pytest.raises(DeletionBlockedError):
            d.apply(LEVEL2, [unit], plan=plan)
        assert d.tree() == tree and d.dump() == dump
        # only the third level, with its confirmation, may take it
        make_irreplaceable(env, unit)
        with pytest.raises(ConfirmationRequiredError):
            d.apply(LEVEL3, [unit])
        assert d.tree() == tree


@pytest.mark.req("EF-BIB-021")
def test_level_two_refuses_an_adopted_original_and_shared_files() -> None:
    """A file adopted in place has no copy of Shelfhold's own; a file that also covers another
    Unit cannot go for one alone: both are refused at the first reading."""
    with export_env() as env:
        d = make_deletion(env)
        series = env.new_series("Dr Stone")
        unit, neighbour = spread_unit(env, series), spread_unit(env, series, "2")
        with env.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE export_record SET state = 'adopted' WHERE export_item_id IN "
                        "(SELECT carrier_id FROM unit_coverage WHERE unit_id = :u)"),
                {"u": unit},
            )  # fmt: skip
        assert d.service.preview(LEVEL2, [unit]).units[0].blocked == ("adopted_original",)
        with env.engine.begin() as connection:  # the neighbour's file covers `unit` too
            item = connection.execute(
                sa.text("SELECT carrier_id FROM unit_coverage WHERE unit_id = :u"), {"u": neighbour}
            ).scalar_one()
            connection.execute(
                sa.text("INSERT INTO unit_coverage (carrier_type, carrier_id, unit_id, position, "
                        "extent, origin) VALUES ('export_item', :i, :u, 1, 'full', 'manual')"),
                {"i": item, "u": unit},
            )  # fmt: skip
        plan = d.service.preview(LEVEL3, [neighbour])
        assert "collateral" in plan.units[0].blocked
        before = d.tree()
        with pytest.raises(DeletionBlockedError):
            d.apply(LEVEL3, [neighbour], plan=plan)
        assert d.tree() == before


# -- the bin --------------------------------------------------------------------------------------


def age_bins(d: Deletion, days: int) -> None:
    """The bin entries are `days` old: the sweep reads the age from the file system."""
    then = time.time() - days * 86400
    for root in (d.env.lib.library, d.env.lib.store):
        for entry in (root / ".shelfhold" / "recycle").glob("*"):
            os.utime(entry, (then, then))


def sweeper(d: Deletion, **kw: object) -> RecycleSweeper:
    journal = IntentJournal(d.env.engine, d.env.lib.fsops, clock=d.env.clock)
    return RecycleSweeper(
        d.env.lib.fsops,
        journal,
        [str(d.env.lib.library), str(d.env.lib.store)],
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.req("EF-BIB-021", "ENF-OPS-009")
def test_trash_retention_n_days() -> None:
    """A deletion of content goes through the bin: nothing is erased before its 30 days, and a
    restore gives back exactly what was there, with the same hash."""
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        plan = d.service.preview(LEVEL3, [unit])
        hashes = sorted(f.file_hash for f in plan.units[0].files)
        report = d.apply(LEVEL3, [unit], plan=plan)
        assert report.moved == 3  # type: ignore[attr-defined]
        rows = d.rows("SELECT kind, level, ref, purge_after > deleted_at FROM trash_item")
        assert [(r[0], r[1], r[2], r[3]) for r in rows] == [("unit", "delete_content", unit, 1)] * 3
        [(days,)] = d.rows(
            "SELECT CAST(julianday(purge_after) - julianday(deleted_at) AS INTEGER) "
            "FROM trash_item LIMIT 1"
        )
        assert days == 30
        age_bins(d, 29)
        assert sweeper(d).sweep().removed == 0 and len(d.binned()) == 3  # young: kept
        assert sorted(file_hash(p) for p in d.binned()) == hashes
        age_bins(d, 31)
        swept = sweeper(d).sweep()
        assert swept.removed >= 1 and d.binned() == []  # only after the term
        assert d.rows("SELECT count(*) FROM trash_item") == [(0,)]  # the index follows the bin
        with pytest.raises(RestoreBlockedError):  # too late: nothing to put back
            d.service.restore(unit, principal=ADMIN, now=d.now)


@pytest.mark.req("EF-BIB-021")
def test_restore_gives_back_exactly_the_state_before() -> None:
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        tree = d.tree()
        state = (
            replicas(d, unit),
            d.rows("SELECT * FROM export_record"),
            d.rows("SELECT follow_suppressed, state FROM unit WHERE id = :u", u=unit),
        )
        inodes = {p: p.stat().st_ino for p in (env.lib.library.rglob("*.cbz"))}
        d.apply(LEVEL3, [unit])
        assert d.tree() != tree
        report = d.service.restore(unit, principal=ADMIN, now=d.now)
        assert report.restored == 3
        assert d.tree() == tree  # same paths, same hashes
        assert {p: p.stat().st_ino for p in env.lib.library.rglob("*.cbz")} == inodes
        assert (
            replicas(d, unit),
            d.rows("SELECT * FROM export_record"),
            d.rows("SELECT follow_suppressed, state FROM unit WHERE id = :u", u=unit),
        ) == state
        assert d.rows("SELECT count(*) FROM trash_item") == [(0,)]
        # level 2 is restored the same way
        d.apply(LEVEL2, [unit])
        d.service.restore(unit, principal=ADMIN, now=d.now)
        assert d.tree() == tree and replicas(d, unit) == state[0]


@pytest.mark.req("EF-BIB-021")
def test_restore_refuses_what_would_not_be_exact() -> None:
    """A bin entry that changed, or a path taken since: nothing at all is put back."""
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        d.apply(LEVEL3, [unit])
        tree = d.tree()
        taken = dest_file_path = env.lib.manga / "Kavita" / "Dr Stone" / "Dr Stone - Ch.1.cbz"
        taken.parent.mkdir(parents=True)
        taken.write_bytes(b"somebody else's file")
        with pytest.raises(RestoreBlockedError) as occupied:
            d.service.restore(unit, principal=ADMIN, now=d.now)
        assert occupied.value.reason == "path_taken"
        assert taken.read_bytes() == b"somebody else's file"  # never replaced
        taken.unlink()
        taken.parent.rmdir()
        changed = d.binned()[0]
        changed.write_bytes(changed.read_bytes() + b"tampered")
        with pytest.raises(RestoreBlockedError) as altered:
            d.service.restore(unit, principal=ADMIN, now=d.now)
        assert altered.value.reason == "bytes_changed"
        assert not dest_file_path.exists()
        assert d.tree().keys() == tree.keys() and d.rows("SELECT count(*) FROM replica") == [(0,)]
        for principal in (MEMBER, CHILD, KEY, STALE):
            with pytest.raises(DeletionRefusedError):
                d.service.restore(unit, principal=principal, now=d.now)


@pytest.mark.req("EF-BIB-021")
def test_store_copy_kept_while_retained_backup_references_it(tmp_path: Path) -> None:
    """A copy kept in /store stays in the bin past its 30 days while a retained integrated backup,
    taken since the Unit existed, may be restored and look for it; the other copies go."""
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        d.apply(LEVEL3, [unit])
        age_bins(d, 31)
        backups = tmp_path / "backups"
        backups.mkdir()
        taken = datetime(2026, 11, 10, tzinfo=UTC)
        retained = {"present": True}

        def lookup(since: datetime) -> list[tuple[str, datetime]]:
            return (
                [("shelfhold-backup-20261110T000000Z.tar.age", taken)]
                if retained["present"]
                else []
            )

        report = sweeper(d, backups=lookup).sweep()
        store_left = [p for p in d.binned() if env.lib.store in p.parents]
        assert len(store_left) == 1 and report.kept >= 1  # kept for the backup
        assert [p for p in d.binned() if env.lib.store not in p.parents] == []  # the others went
        assert d.rows("SELECT count(*) FROM trash_item") == [(1,)]
        retained["present"] = False  # the backup was pruned
        assert sweeper(d, backups=lookup).sweep().removed == 1
        assert d.binned() == []


# -- the empty managed folders ---------------------------------------------------------------------


@pytest.mark.req("EF-BIB-021")
def test_empty_managed_folders_removed_after_scan() -> None:
    with export_env() as env:
        d = make_deletion(env)
        series = env.new_series("Dr Stone")
        first, second = spread_unit(env, series, "1"), spread_unit(env, series, "2")
        folder = env.lib.manga / "Kavita" / "Dr Stone"
        d.apply(LEVEL2, [first])
        assert folder.is_dir() and d.scans == []  # the folder still holds the other chapter
        report = d.apply(LEVEL2, [second])
        assert not folder.exists() and report.folders_removed != ()  # type: ignore[attr-defined]
        assert d.order == ["scan:True"]  # the scan was asked while the folder was still there
        assert d.scans == ["/manga/Kavita/Dr Stone"]
        assert (env.lib.manga / "Kavita").is_dir()  # the destination's own folder stays


@pytest.mark.req("EF-BIB-021")
def test_empty_folders_wait_for_a_scan_that_failed() -> None:
    with export_env() as env:
        d = make_deletion(env, fail_scan=True)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        report = d.apply(LEVEL2, [unit])
        folder = env.lib.manga / "Kavita" / "Dr Stone"
        assert folder.is_dir() and report.folders_removed == ()  # type: ignore[attr-defined]
        assert report.folders_kept != ()  # type: ignore[attr-defined]
        # a later pass (after a good scan) takes it, and only if it is still empty
        [(destination, rel)] = [(env.destination_id, "Dr Stone")]
        removed, kept = d.service.remove_empty_folders([(destination, rel)])
        assert removed == (f"{destination}/{rel}",) and kept == () and not folder.exists()


@pytest.mark.req("EF-BIB-021")
def test_adopted_non_empty_folder_never_removed() -> None:
    """A folder with anything untracked in it, a folder of an adopted destination and the folders
    Shelfhold keeps for itself are never removed; an unknown file is never touched."""
    with export_env() as env:
        d = make_deletion(env)
        series = env.new_series("Dr Stone")
        unit = spread_unit(env, series)
        folder = env.lib.manga / "Kavita" / "Dr Stone"
        stranger = folder / "notes.txt"
        stranger.write_text("not a file Shelfhold knows")
        keep = env.lib.manga / "Kavita" / "Handmade"  # a folder nobody exported into
        keep.mkdir()
        plan = d.service.preview(LEVEL3, [unit])
        assert plan.folders == ()  # not announced: it would not be empty
        report = d.apply(LEVEL3, [unit], plan=plan)
        assert folder.is_dir() and stranger.read_text() == "not a file Shelfhold knows"
        assert keep.is_dir() and report.folders_removed == ()  # type: ignore[attr-defined]
        assert (env.lib.library / ".shelfhold").is_dir()
        # even asked by hand, a folder that holds something is kept; so is a reserved one
        removed, kept = d.service.remove_empty_folders(
            [(env.destination_id, "Dr Stone"), (env.destination_id, ".shelfhold")]
        )
        assert removed == () and folder.is_dir()
        assert len(kept) == 2
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        folder = env.lib.manga / "Kavita" / "Dr Stone"
        with env.engine.begin() as connection:  # an adopted library: its folders are not ours
            connection.execute(sa.update(export.shelf_destination).values(adopted=True))
        plan = d.service.preview(LEVEL2, [unit])
        assert plan.folders == ()
        d.apply(LEVEL2, [unit], plan=plan)
        assert folder.is_dir() and list(folder.iterdir()) == []  # empty, and still there
        removed, kept = d.service.remove_empty_folders([(env.destination_id, "Dr Stone")])
        assert removed == () and folder.is_dir() and kept != ()


@pytest.mark.req("EF-BIB-021")
def test_unknown_and_unmanaged_files_are_never_touched() -> None:
    """Only the files the registry knows move: a stranger in the same folder, a file in /store
    that no row names and a tracked path whose file is gone are left alone."""
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        stranger = env.lib.manga / "Kavita" / "Dr Stone" / "cover.jpg"
        stranger.write_bytes(b"jpg")
        orphan = env.lib.store / "units" / "zz" / "orphan.cbz"
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(b"no row names me")
        d.apply(LEVEL3, [unit])
        assert stranger.read_bytes() == b"jpg" and orphan.read_bytes() == b"no row names me"
        assert not d.service.preview(LEVEL3, [unit]).units[0].files  # nothing registered is left


# -- access ---------------------------------------------------------------------------------------


@pytest.mark.req("EF-BIB-021", "EF-USR-024")
def test_deletion_is_a_reserved_action() -> None:
    """A member, a child, an API key and a stale session are refused at every level, before any
    write; the audit of an allowed deletion names the actor and no title."""
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        dump = d.dump()
        for level in DeletionLevel:
            plan = d.service.preview(level, [unit])
            for principal in (MEMBER, CHILD, KEY, STALE):
                with pytest.raises(DeletionRefusedError):
                    d.apply(level, [unit], plan=plan, principal=principal)
                assert d.dump() == dump
        d.apply(LEVEL2, [unit])
        [(actor, action, target)] = d.rows("SELECT actor_id, action, target FROM audit")
        assert (actor, action) == (ADMIN.actor_id, "unit.delete")
        assert unit in str(target) and "Dr Stone" not in str(target)


# -- concurrency -----------------------------------------------------------------------------------


@pytest.mark.req("EF-BIB-021", "EF-EXP-042")
def test_deletion_waits_for_an_export_and_a_save_holding_the_work() -> None:
    """The lock of the Work is the one an export and "Mettre à l'abri" hold: while one of them
    holds it, the deletion is refused as busy and nothing is written."""
    with export_env() as env:
        d = make_deletion(env)
        series = env.new_series("Dr Stone")
        unit = spread_unit(env, series)
        slow = DeletionService(
            env.engine, env.lib.fsops, clock=env.clock, locate=env.lib.locate, lock_wait=0.05
        )
        plan = slow.preview(LEVEL3, [unit])
        locks = ResourceLocks(env.engine, clock=env.clock)
        tree, dump = d.tree(), d.dump()
        with locks.hold_resources(new_ulid(), [work_resource(series.work_id)], timeout=1.0):
            with pytest.raises(LockBusyError):
                slow.apply(LEVEL3, [unit], if_match=plan.version, typed=None, principal=ADMIN,
                           now=d.now)  # fmt: skip
            assert d.tree() == tree
        assert d.dump() == dump
        slow.apply(LEVEL3, [unit], if_match=plan.version, typed=None, principal=ADMIN, now=d.now)
        assert replicas(d, unit) == [] and d.rows("SELECT count(*) FROM resource_lock") == [(0,)]


@pytest.mark.req("EF-BIB-021")
def test_a_file_under_an_open_intent_blocks_the_deletion() -> None:
    """Another operation already moves that file (an open intent): the preview says so, the
    application refuses, and the intent is left as it was."""
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        journal = IntentJournal(env.engine, env.lib.fsops, clock=env.clock)
        intent = journal.record(op="recycle", src=str(dest_file(env, unit)))
        plan = d.service.preview(LEVEL2, [unit])
        assert plan.units[0].blocked == ("open_intent",)
        tree = d.tree()
        with pytest.raises(DeletionBlockedError):
            d.apply(LEVEL2, [unit], plan=plan)
        assert d.tree() == tree and open_intents(env) == [intent]


@pytest.mark.req("EF-BIB-021")
def test_a_move_that_fails_is_finished_by_its_intent_into_the_named_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bin entry is recorded before the move: when the move fails once, the intent's replay
    puts the file in that very entry, so the bin index still points at it (and a restore works)."""
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        real = type(env.lib.fsops).recycle
        calls: list[str] = []

        def flaky(self: object, local: str, *, entry: str | None = None) -> str:
            calls.append(local)
            if len(calls) == 1:
                raise OSError("the disk hiccuped")
            return real(self, local, entry=entry)  # type: ignore[arg-type]

        monkeypatch.setattr(type(env.lib.fsops), "recycle", flaky)
        report = d.apply(LEVEL2, [unit])
        assert report.pending == () and report.moved == 1  # type: ignore[attr-defined]
        [(path,)] = d.rows("SELECT path FROM trash_item")
        assert (env.lib.library / ".shelfhold" / "recycle" / str(path)).is_file()
        assert open_intents(env) == []
        monkeypatch.undo()
        assert d.service.restore(unit, principal=ADMIN, now=d.now).restored == 1


@pytest.mark.req("EF-BIB-021")
def test_a_blocked_root_stops_the_deletion_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sentinel's word comes first (`require_delete_allowed`): a root that does not accept
    deletions stops the run before an intent, a row or a file."""
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        plan = d.service.preview(LEVEL3, [unit])
        dump = d.dump()

        def refuse(self: object, local: str) -> None:
            raise PermissionError("blocked")

        monkeypatch.setattr(type(env.lib.fsops), "require_delete_allowed", refuse)
        with pytest.raises(PermissionError):
            d.apply(LEVEL3, [unit], plan=plan)
        assert d.dump() == dump


@pytest.mark.req("EF-BIB-021")
def test_a_read_only_root_is_never_written(monkeypatch: pytest.MonkeyPatch) -> None:
    """A copy under an adoption root (read-only) is listed as untouched and stays where it is,
    at every level; the managed copies elsewhere still go at the third."""
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        served = dest_file(env, unit)
        real = type(env.lib.fsops).is_read_only
        monkeypatch.setattr(
            type(env.lib.fsops),
            "is_read_only",
            lambda self, local: "/Kavita/" in local or real(self, local),
        )
        for level in (LEVEL2, LEVEL3):
            plan = d.service.preview(level, [unit])
            assert "read_only_root" in plan.units[0].untouched
            assert all(f.place != "destination" for f in plan.units[0].files)
        d.apply(LEVEL2, [unit])
        assert served.exists()
        d.apply(LEVEL3, [unit])
        assert served.exists() and len(d.binned()) == 2  # /library and /store went, not this one
        assert [r[1] for r in replicas(d, unit)] == ["destination"]


@pytest.mark.req("EF-BIB-021")
def test_a_restore_that_fails_midway_leaves_nothing_half_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        d.apply(LEVEL3, [unit])
        tree, dump = d.tree(), d.dump()
        real = type(env.lib.fsops).restore_from_bin
        calls: list[str] = []

        def second_fails(self: object, local: str, path: str) -> None:
            calls.append(local)
            if len(calls) == 2:
                raise OSError("the disk went away")
            real(self, local, path)  # type: ignore[arg-type]

        monkeypatch.setattr(type(env.lib.fsops), "restore_from_bin", second_fails)
        with pytest.raises(OSError, match="went away"):
            d.service.restore(unit, principal=ADMIN, now=d.now)
        assert len(calls) == 2
        assert d.tree().keys() == tree.keys()  # the first file left again for its entry
        assert d.dump() == dump  # no row came back, no bin row was forgotten
        monkeypatch.undo()
        assert d.service.restore(unit, principal=ADMIN, now=d.now).restored == 3


@pytest.mark.req("EF-BIB-021")
def test_a_deleted_unit_is_not_exported_or_followed_again() -> None:
    """The records say `recycled` and the Unit is no longer followed: the next export puts
    nothing back in the destination and the follow cycle has no reason to fetch it."""
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        d.apply(LEVEL3, [unit])
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert list((env.lib.manga / "Kavita").rglob("*.cbz")) == []
        assert d.rows("SELECT state FROM export_record") == [("recycled",)]
        assert d.rows("SELECT follow_suppressed FROM unit WHERE id = :u", u=unit) == [(1,)]
