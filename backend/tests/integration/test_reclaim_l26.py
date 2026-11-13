"""M3 L26 (EF-BIB-030, ENF-OPS-006): "Empty the trash now" and the early purge under disk pressure.

Real files, real SQLite, real pipeline (`deletion_env`). Every "never" is a test that tries to
break it: a file outside the bins, a link, the admin's own file, an entry held by an open intent,
an irreplaceable Unit, a young entry, a key, a member, a stale session, a wrong word, two runs at
once, a restore on its way, a full disk during an export.
"""

import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from shelfhold.export.application.reclaim import (
    EARLY_PURGE_MIN_AGE_S,
    EMPTY_TRASH_PHRASE,
    BinReclaimer,
    TrashConfirmationError,
    TrashEmptyRefusedError,
)

from shelfhold.export.application.intents import IntentJournal
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.fsops.errors import (
    EmitterConditionError,
    EmitterPerimeterError,
)
from shelfhold.infrastructure.observability.disk_guard import DiskLowError
from shelfhold.library.public import DeletionLevel
from tests.deletion_env import Deletion, make_deletion, make_irreplaceable, spread_unit
from tests.export_env import export_env
from tests.purge_env import ADMIN, KEY, STALE

pytestmark = pytest.mark.req("EF-BIB-030")

DAY = 86400
MEMBER = Principal(
    "01J00000000000000000000MEM", "member", "member", Credential.SESSION,
    session_id="01J0000000000000000000SES1", auth_time=datetime(2026, 11, 5, 8, 0, tzinfo=UTC),
)  # fmt: skip
CHILD = Principal(
    "01J00000000000000000000CHD", "child", "child", Credential.SESSION,
    session_id="01J0000000000000000000SES2", auth_time=datetime(2026, 11, 5, 8, 0, tzinfo=UTC),
)  # fmt: skip


def reclaimer(d: Deletion, **kw: object) -> BinReclaimer:
    journal = IntentJournal(d.env.engine, d.env.lib.fsops, clock=d.env.clock)
    return BinReclaimer(
        d.env.lib.fsops, journal, [str(d.env.lib.library), str(d.env.lib.store)],
        clock=d.env.clock, fresh_auth_s=900, **kw,  # type: ignore[arg-type]
    )  # fmt: skip


def bin_dirs(d: Deletion) -> list[Path]:
    return sorted(
        p
        for root in (d.env.lib.library, d.env.lib.store)
        for p in (root / ".shelfhold" / "recycle").glob("*")
    )


def age_entry(entry: Path, days: float) -> None:
    then = time.time() - days * DAY
    os.utime(entry, (then, then))


def binned_unit(d: Deletion, series: str = "Dr Stone", *, days: float = 1) -> str:
    """One Unit deleted at the third level, its bin entries (and their terms) `days` old."""
    known = set(bin_dirs(d))
    unit = spread_unit(d.env, d.env.new_series(series))
    d.apply(DeletionLevel.DELETE_CONTENT, [unit])
    for entry in set(bin_dirs(d)) - known:
        age_entry(entry, days)
    now = d.now
    with d.env.engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE trash_item SET purge_after = :p, deleted_at = :d WHERE ref = :u"),
            {
                "p": (now + timedelta(days=30 - days)).replace(tzinfo=None).isoformat(sep=" "),
                "d": (now - timedelta(days=days)).replace(tzinfo=None).isoformat(sep=" "),
                "u": unit,
            },
        )
    return unit


def entries_of(d: Deletion, unit: str) -> list[Path]:
    names = {
        r[0].split("/", 1)[0] for r in d.rows("SELECT path FROM trash_item WHERE ref = :u", u=unit)
    }
    return [p for p in bin_dirs(d) if p.name in names]


def audit(d: Deletion, action: str) -> list[tuple[object, ...]]:
    return d.rows("SELECT actor_id, target, session_id FROM audit WHERE action = :a", a=action)


# -- « Vider la corbeille maintenant » ---------------------------------------------------------


def test_empty_trash_now() -> None:
    """Every bin is emptied at once, whatever its age; the index follows; the rest of the tree is
    exactly what it was; one audit line with the actor and the count, no name."""
    with export_env() as env:
        d = make_deletion(env)
        unit = binned_unit(d, days=1)
        outside = d.tree()  # without the bins: the copies that stayed (destination)
        assert bin_dirs(d), "the deletion put the files in the bin"
        before_tree = {k: v for k, v in outside.items() if ".shelfhold/recycle" not in k}

        report = reclaimer(d).empty_now(principal=ADMIN, typed=EMPTY_TRASH_PHRASE)

        assert report.removed >= 1 and bin_dirs(d) == [] and d.binned() == []
        assert d.rows("SELECT count(*) FROM trash_item") == [(0,)]
        after_tree = {k: v for k, v in d.tree().items() if ".shelfhold/recycle" not in k}
        assert after_tree == before_tree  # nothing but the bins was touched
        [(actor, target, _session)] = audit(d, "trash.emptied")
        assert actor == ADMIN.actor_id and target == f"entries:{report.removed}"
        assert unit not in repr(audit(d, "trash.emptied"))
        assert [i for i in d.rows("SELECT state FROM fs_intent") if i != ("done",)] == []


@pytest.mark.parametrize(
    "who", [KEY, STALE, MEMBER, CHILD], ids=["api-key", "stale-session", "member", "child"]
)
def test_empty_trash_refused_without_the_right(who: Principal) -> None:
    with export_env() as env:
        d = make_deletion(env)
        binned_unit(d)
        before = d.dump()

        with pytest.raises(TrashEmptyRefusedError):
            reclaimer(d).empty_now(principal=who, typed=EMPTY_TRASH_PHRASE)

        assert d.dump() == before and bin_dirs(d)  # nothing removed, no audit, no intent
        assert audit(d, "trash.emptied") == []


@pytest.mark.parametrize("typed", [None, "", "empty", "EMPTY-TRASH", " empty-trash x", "yes"])
def test_empty_trash_needs_the_typed_phrase(typed: str | None) -> None:
    with export_env() as env:
        d = make_deletion(env)
        binned_unit(d)
        before = d.dump()

        with pytest.raises(TrashConfirmationError):
            reclaimer(d).empty_now(principal=ADMIN, typed=typed)

        assert d.dump() == before and bin_dirs(d)


def test_empty_trash_never_touches_what_is_not_the_bin(tmp_path: Path) -> None:
    """The admin's own files beside the bin, a link inside the bin that points at one, a link
    that stands for a bin entry, the sentinel, the journals and the lease: all still there."""
    with export_env() as env:
        d = make_deletion(env)
        binned_unit(d)
        library, store = env.lib.library, env.lib.store
        mine = library / "my notes.txt"
        mine.write_text("the admin's own file")
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "precious.txt").write_text("precious")
        recycle = library / ".shelfhold" / "recycle"
        (recycle / "linked-entry").symlink_to(outside)  # an entry that is a link to a folder
        entry = recycle / "01J00000000000000000FAKE00"
        entry.mkdir()
        (entry / "inner-link").symlink_to(outside / "precious.txt")
        (entry / "inner-dir-link").symlink_to(outside)
        host = {
            p: p.read_bytes()
            for p in (library / ".shelfhold-mount-id", store / ".shelfhold-mount-id")
        }

        reclaimer(d).empty_now(principal=ADMIN, typed=EMPTY_TRASH_PHRASE)

        assert mine.read_text() == "the admin's own file"
        assert (outside / "precious.txt").read_text() == "precious"  # a link is never followed
        assert sorted(p.name for p in outside.iterdir()) == ["precious.txt"]
        assert {p: p.read_bytes() for p in host} == host
        assert (library / ".shelfhold").is_dir()
        # the real entries are gone; a link that stands for an entry is left alone, not followed
        assert [p.name for p in bin_dirs(d) if not p.is_symlink() and p.name != entry.name] == []


def test_empty_trash_keeps_what_is_held_and_says_why() -> None:
    """An irreplaceable Unit's entry, an entry that an open intent names (a restore on its way):
    kept, counted, never removed in silence."""
    with export_env() as env:
        d = make_deletion(env)
        unit = binned_unit(d)
        make_irreplaceable(env, unit)

        report = reclaimer(d).empty_now(principal=ADMIN, typed=EMPTY_TRASH_PHRASE)

        assert report.removed == 0 and report.held_irreplaceable >= 1 and bin_dirs(d)
        assert d.rows("SELECT count(*) FROM trash_item")[0][0] >= 1


def test_empty_trash_leaves_an_entry_that_a_restore_is_using() -> None:
    """A purge during a restoration: the restore holds an open intent on its entry; the empty
    keeps that entry and the restore then completes."""
    with export_env() as env:
        d = make_deletion(env)
        unit = binned_unit(d)
        entry = bin_dirs(d)[0]
        journal = IntentJournal(env.engine, env.lib.fsops, clock=env.clock)
        intent = journal.record(op="unbin", src=str(entry))  # what a restore opens first
        journal.mark_applying(intent)

        report = reclaimer(d).empty_now(principal=ADMIN, typed=EMPTY_TRASH_PHRASE)

        assert entry.exists() and report.kept >= 1
        journal.cancel(intent)
        assert d.service.restore(unit, principal=ADMIN, now=d.now).restored == 3


def test_two_simultaneous_empties_remove_each_entry_once_and_raise_nothing() -> None:
    with export_env() as env:
        d = make_deletion(env)
        for name in ("Dr Stone", "Gantz", "Berserk"):
            binned_unit(d, name)
        entries = len(bin_dirs(d))
        gate = threading.Barrier(2)
        results: list[object] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                gate.wait(timeout=10)
                results.append(reclaimer(d).empty_now(principal=ADMIN, typed=EMPTY_TRASH_PHRASE))
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert errors == [] and len(results) == 2
        assert sum(r.removed for r in results) == entries  # type: ignore[attr-defined]
        assert bin_dirs(d) == [] and d.rows("SELECT count(*) FROM trash_item") == [(0,)]


def test_the_cure_is_never_blocked_by_the_disk_guard() -> None:
    """The disk is full and every write is refused: emptying the trash still works (it removes,
    it never adds)."""
    with export_env() as env:
        d = make_deletion(env)
        binned_unit(d)

        def refuse() -> None:
            raise DiskLowError("library_1")

        env.lib.fsops._write_gate = refuse  # type: ignore[attr-defined]
        report = reclaimer(d).empty_now(principal=ADMIN, typed=EMPTY_TRASH_PHRASE)
        assert report.removed >= 1 and bin_dirs(d) == []


# -- the early purge under disk pressure ------------------------------------------------------


def test_disk_pressure_purges_oldest_never_irreplaceable() -> None:
    """Oldest first, one entry after the other until the pressure is over; an irreplaceable
    Unit's entry stays whatever its age; an entry younger than the minimum stays."""
    with export_env() as env:
        d = make_deletion(env)
        old = binned_unit(d, "Dr Stone", days=40)
        kept = binned_unit(d, "Gantz", days=50)  # older still, but irreplaceable
        make_irreplaceable(env, kept)
        mid = binned_unit(d, "Berserk", days=20)
        young = binned_unit(d, "Vagabond", days=2)  # under the minimum: never
        left = {"entries": 3}  # the free space is back once the first Unit is gone (3 entries)

        def pressure() -> bool:
            return left["entries"] > 0

        env.lib.fsops.on_disk_pressure(lambda _root: pressure())
        report = reclaimer(d).early_purge(
            pressure=pressure, after_each=lambda: left.update(entries=left["entries"] - 1)
        )

        assert report.removed == 3 and entries_of(d, old) == []  # the oldest replaceable went
        assert entries_of(d, mid) and entries_of(d, kept) and entries_of(d, young)
        assert report.held_irreplaceable >= 1
        [(actor, target, _s)] = audit(d, "trash.early_purged")  # audited: a count, no name
        assert actor == "system" and target == "entries:3"
        assert kept not in target


def test_early_purge_does_nothing_without_pressure_and_is_bounded() -> None:
    with export_env() as env:
        d = make_deletion(env)
        for name in ("Dr Stone", "Gantz", "Berserk", "Vagabond"):
            binned_unit(d, name, days=40)
        entries = len(bin_dirs(d))
        r = reclaimer(d)

        calm = r.early_purge(pressure=lambda: False)
        assert calm.removed == 0 and len(bin_dirs(d)) == entries

        env.lib.fsops.on_disk_pressure(lambda _root: True)
        bounded = r.early_purge(pressure=lambda: True, limit=2)
        assert bounded.removed == 2 and len(bin_dirs(d)) == entries - 2


def test_early_purge_refuses_a_young_entry_at_the_fsops_level_too() -> None:
    """The age is read from the file system by fsops, whatever the caller says; so are the
    perimeter and the pressure. Three attacks on the system emitter itself."""
    with export_env() as env:
        d = make_deletion(env)
        binned_unit(d, days=2)
        fsops = env.lib.fsops
        [young, *_] = bin_dirs(d)
        fsops.on_disk_pressure(lambda _root: True)
        with pytest.raises(EmitterConditionError):  # under the minimum age
            fsops.system_remove_tree("disk_guard_purge", str(young))
        age_entry(young, EARLY_PURGE_MIN_AGE_S / DAY + 1)
        fsops.on_disk_pressure(lambda _root: False)
        with pytest.raises(EmitterConditionError):  # no pressure
            fsops.system_remove_tree("disk_guard_purge", str(young))
        fsops.on_disk_pressure(lambda _root: True)
        mine = env.lib.library / "my notes.txt"
        mine.write_text("mine")
        with pytest.raises(EmitterPerimeterError):  # outside the bin
            fsops.system_remove_tree("disk_guard_purge", str(mine))
        with pytest.raises(EmitterPerimeterError):  # the bin itself, not an entry of it
            fsops.system_remove_tree("disk_guard_purge", str(env.lib.library / ".shelfhold"))
        assert mine.exists() and young.exists()
        fsops.system_remove_tree("disk_guard_purge", str(young))  # now it is allowed
        assert not young.exists()


def test_pressure_during_an_export_leaves_the_intents_of_the_export_alone() -> None:
    """The disk fills while an upgrade holds an open intent on its old version in the bin: the
    early purge keeps that entry (the only copy may be there) and removes the others."""
    with export_env() as env:
        d = make_deletion(env)
        binned_unit(d, "Dr Stone", days=40)
        binned_unit(d, "Gantz", days=40)
        [held, *others] = bin_dirs(d)
        journal = IntentJournal(env.engine, env.lib.fsops, clock=env.clock)
        intent = journal.record(op="delete", src=str(held))
        journal.mark_applying(intent)
        env.lib.fsops.on_disk_pressure(lambda _root: True)

        report = reclaimer(d).early_purge(pressure=lambda: True)

        assert held.exists() and report.kept >= 1
        assert all(not p.exists() for p in others)
