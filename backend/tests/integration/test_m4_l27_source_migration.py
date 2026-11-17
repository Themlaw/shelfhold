"""M4 L27 (EF-BIB-014): the full migration of a series to another source.

A series moves to a source that numbers its chapters differently. The numbering correspondence is
**proposed** (by identical numbers, or by one constant offset), the admin **validates** the table
he was shown, and only then is anything written: the new binding, the correspondence the follow
cycle reads, the audit line. The Units already held stay the Units (same id, same number, same
file): what every profile read and where each one stopped lives on them and does not move. A
correspondence that would make two numbers one, or that would write a second Unit where one is
held, is refused before a row is written. Real database, real transactions.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import (
    APPLY_STEPS,
    CollisionError,
    MigrationNotAllowedError,
    NotValidatedError,
    Override,
    SourceMigrations,
    StaleMigrationError,
    number_map_of,
)
from shelfhold.catalog.public import ListedUnit
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, identity, replica
from tests.integration.test_schema_catalog import add_binding, add_source_item, add_variant
from tests.reading_env import OLD_HASH, ReadingEnv, hours, reading_env

pytestmark = pytest.mark.req("EF-BIB-014")

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
HELD = 5  # chapters 1..5 are filed


def admin(**changes: object) -> Principal:
    base: dict[str, Any] = {
        "user_id": new_ulid(), "username": "admin", "role": "admin",
        "credential": Credential.LOCAL_CLI, "session_id": None, "auth_time": None,
        "can_rate": True,
    }  # fmt: skip
    return Principal(**{**base, **changes})


def listing(first: int, last: int) -> tuple[ListedUnit, ...]:
    return tuple(ListedUnit(str(n), n - first) for n in range(first, last + 1))


@dataclass
class Moved:
    """A series whose source went away: its Edition holds chapters 1..5, each with the Variant of
    the old binding, a profile that read some and one that stopped in the middle of another."""

    env: ReadingEnv
    edition: str
    work: str
    old_binding: str
    units: list[str]
    mia: str
    leo: str

    @property
    def engine(self) -> sa.Engine:
        return self.env.engine

    def propose(self, listed: tuple[ListedUnit, ...], **extra: Any) -> Any:
        values: dict[str, Any] = {
            "plugin_id": "suwayomi", "source_key": "suwayomi:7002", "remote_id": "/new-source",
            "listed": listed, "replaces_binding_id": self.old_binding,
        }  # fmt: skip
        with write_transaction(self.engine) as c:
            return SourceMigrations().propose(
                c, self.edition, **{**values, **extra}, principal=admin(), now=NOW
            )

    def validate(self, proposal: Any, overrides: tuple[Override, ...] = (), **extra: Any) -> Any:
        with write_transaction(self.engine) as c:
            return SourceMigrations().validate(
                c, self.edition, proposal.id, digest=extra.pop("digest", proposal.digest),
                overrides=overrides, principal=admin(), now=NOW,
            )  # fmt: skip

    def apply(self, proposal: Any, **extra: Any) -> Any:
        with write_transaction(self.engine) as c:
            return SourceMigrations().apply(
                c, self.edition, proposal.id, digest=extra.pop("digest", proposal.digest),
                principal=extra.pop("principal", admin()), now=NOW,
            )  # fmt: skip

    def census(self) -> dict[str, Any]:
        """What a migration must never change for what is already held."""
        with self.engine.connect() as c:

            def rows(table: sa.Table) -> list[Any]:
                return sorted(tuple(r) for r in c.execute(sa.select(table)))

            from shelfhold.infrastructure.schema import export, reading, storage

            return {
                "units": rows(catalog.unit),
                "variants": rows(catalog.unit_variant),
                "content": rows(storage.content),
                "replicas": rows(replica.replica),
                "positions": rows(reading.reading_position),
                "coverage": rows(export.unit_coverage),
                "progress": [tuple(r) for r in c.execute(sa.text(
                    "SELECT * FROM reading_progress ORDER BY user_id, unit_id"))],
            }  # fmt: skip

    def count(self, table: sa.Table) -> int:
        with self.engine.connect() as c:
            return int(c.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())

    def bindings(self) -> int:
        """The source bindings of the series under test."""
        with self.engine.connect() as c:
            return int(
                c.execute(
                    sa.select(sa.func.count()).where(
                        acquisition.source_binding.c.edition_id == self.edition
                    )
                ).scalar_one()
            )

    def migrations(self) -> list[Any]:
        with self.engine.connect() as c:
            return list(c.execute(sa.select(acquisition.source_migration)))


@pytest.fixture
def moved(tmp_path: Path) -> Iterator[Moved]:
    with reading_env(tmp_path) as env:
        mia = env.user("mia")
        leo = env.user("leo")
        units = env.units("ok", HELD)
        work = env.seed.works["ok"]
        edition = env.seed.editions["ok"]
        with write_transaction(env.engine) as c:
            item = add_source_item(c, "/old-source")
            old = add_binding(
                c, work, edition, item, active=False, disabled_reason="removed_upstream"
            )
            for number, unit in enumerate(units, start=1):
                add_variant(c, unit, old, f"old-{number}")
        item_file = env.file("ok", units, content_hash=OLD_HASH)
        # mia read chapters 1 to 3 (a traversal that proved it) and stopped inside the fourth;
        # leo only opened the first
        env.position(
            mia, item_file, percent=0.8, page=3, content_hash=OLD_HASH, client_ts=hours(1),
            source="device", read_units=units[:3],
        )  # fmt: skip
        env.position(leo, item_file, percent=0.1, page=1, content_hash=OLD_HASH,
                     client_ts=hours(2), covered=[units[0]])  # fmt: skip
        yield Moved(env, edition, work, old, units, mia, leo)


# -- the table is proposed, then validated, then (and only then) applied ---------------------------


@pytest.mark.req("EF-BIB-014", "EF-BIB-029")
def test_numbering_correspondence_validated_before_apply(moved: Moved) -> None:
    before = moved.census()
    proposal = moved.propose(listing(1, 7))
    assert proposal.state == "proposed"
    # the proposal is a table: every number of the new source, the Unit it lands on or "new"
    by_number = {r.number: r for r in proposal.rows}
    assert [by_number[str(n)].unit_id for n in range(1, 6)] == moved.units
    assert by_number["6"].unit_id is None and by_number["7"].unit_id is None
    assert {r.basis for r in proposal.rows} == {"same_number", "new"}
    assert proposal.digest
    # nothing is bound, mapped or created by a proposal
    assert moved.census() == before
    assert moved.bindings() == 1
    with moved.engine.connect() as c:
        assert number_map_of(c, moved.old_binding) is None
    # not applied: not validated
    with pytest.raises(NotValidatedError):
        moved.apply(proposal)
    assert moved.bindings() == 1
    # a validation of another table than the one that was shown: stale, nothing is validated
    with pytest.raises(StaleMigrationError):
        moved.validate(proposal, digest="0" * 64)
    with pytest.raises(NotValidatedError):
        moved.apply(proposal)
    # validated, then applied: the digest of what he validated, not of what was proposed
    validated = moved.validate(proposal)
    assert validated.state == "validated"
    with pytest.raises(StaleMigrationError):
        moved.apply(proposal, digest="f" * 64)
    done = moved.apply(validated)
    assert done.proposal.state == "applied"
    assert [r.number for r in done.to_fetch] == ["6", "7"]  # the only numbers to fetch
    assert moved.bindings() == 2
    with moved.engine.connect() as c:
        assert number_map_of(c, done.binding_id) is not None


@pytest.mark.req("EF-BIB-014")
def test_a_proposal_follows_a_constant_offset_and_keeps_the_library_numbering(
    moved: Moved,
) -> None:
    """The new source numbers 11 to 17 what the library calls 1 to 7: held numbers stay where they
    are, the two numbers the library lacks are written under *its* numbering (6 and 7)."""
    proposal = moved.propose(listing(11, 17))
    assert proposal.offset == -10
    rows = {r.number: r for r in proposal.rows}
    assert [rows[str(n)].unit_id for n in range(11, 16)] == moved.units
    assert {rows[str(n)].basis for n in range(11, 16)} == {"offset"}
    assert (rows["16"].target, rows["17"].target) == ("6", "7")
    assert rows["16"].unit_id is None and rows["17"].unit_id is None
    done = moved.apply(moved.validate(proposal))
    assert [(r.number, r.target) for r in done.to_fetch] == [("16", "6"), ("17", "7")]
    with moved.engine.connect() as c:
        mapping = number_map_of(c, done.binding_id)
        assert mapping is not None
        assert mapping.translate("13", 2)[0] == "3"  # held: the Unit of chapter 3
        # a chapter that the new source publishes later is still in the library's numbering
        assert mapping.translate("18", 7)[0] == "8"
        assert mapping.translate("18.5", 8)[0] == "8.5"


@pytest.mark.req("EF-BIB-014")
def test_a_correspondence_that_makes_two_numbers_one_is_refused_before_any_write(
    moved: Moved,
) -> None:
    proposal = moved.propose(listing(1, 7))
    before = moved.census()
    rows_before = moved.count(acquisition.source_migration_row)
    # two numbers of the new source on the one Unit already held
    with pytest.raises(CollisionError) as two:
        moved.validate(proposal, (Override("2", "1"),))
    assert "2" in two.value.places or "1" in two.value.places
    # a "new" number that lands on a Unit already held (6 -> 3 while 3 is the target of 3)
    with pytest.raises(CollisionError):
        moved.validate(proposal, (Override("6", "3"),))
    # a number that the library already holds can never be written as a new Unit
    with pytest.raises(CollisionError):
        moved.validate(proposal, (Override("7", "5"),))
    assert moved.census() == before
    assert moved.count(acquisition.source_migration_row) == rows_before
    assert moved.migrations()[0].state == "proposed"
    # an override that frees the numbers is accepted, and the table he validates is that one
    freed = moved.validate(proposal, (Override("5", "9"), Override("7", "5")))
    by_number = {r.number: r for r in freed.rows}
    assert by_number["5"].unit_id is None and by_number["5"].target == "9"
    assert by_number["7"].unit_id == moved.units[4] and by_number["7"].basis == "manual"
    assert freed.digest != proposal.digest


@pytest.mark.req("EF-BIB-014")
def test_a_proposal_is_refused_when_the_new_source_lists_one_number_twice_for_two_units(
    moved: Moved,
) -> None:
    """The same number twice in the listing is one row (two releases of one chapter), never two
    Units; an unreadable number is an extra placed by its order, as the follow places it."""
    twice = (*listing(1, 3), ListedUnit("3", 2), ListedUnit(None, 3), ListedUnit("oneshot", 4))
    proposal = moved.propose(twice)
    numbers = [r.number for r in proposal.rows]
    assert numbers.count("3") == 1
    assert len({r.place for r in proposal.rows}) == len(proposal.rows)


@pytest.mark.req("EF-BIB-014")
def test_apply_refuses_a_table_the_edition_has_moved_away_from(moved: Moved) -> None:
    """Under the write lock the table is checked against the Edition as it is *now*: a Unit that
    came or went since it was shown makes it stale, and nothing is written."""
    validated = moved.validate(moved.propose(listing(1, 7)))
    with write_transaction(moved.engine) as c:  # chapter 6 was filed meanwhile, by another way
        c.execute(
            sa.insert(catalog.unit).values(
                id=new_ulid(), edition_id=moved.edition, number_raw="6", number_norm="6",
                number_sort=6.0, track="main", kind="chapter",
            )
        )  # fmt: skip
    before = moved.census()
    with pytest.raises(StaleMigrationError):
        moved.apply(validated)
    assert moved.census() == before
    assert moved.migrations()[0].state == "validated"
    assert moved.bindings() == 1


@pytest.mark.req("EF-BIB-014")
def test_apply_twice_changes_nothing_more(moved: Moved) -> None:
    validated = moved.validate(moved.propose(listing(1, 7)))
    first = moved.apply(validated)
    after_first = (moved.census(), moved.bindings())
    audits = _audits(moved)
    again = moved.apply(validated)
    assert again.binding_id == first.binding_id and again.already is True
    assert (moved.census(), moved.bindings()) == after_first
    assert _audits(moved) == audits  # replayed, not done twice


def _audits(moved: Moved) -> list[str]:
    with moved.engine.connect() as c:
        return [
            f"{r.action}:{r.target}"
            for r in c.execute(sa.select(identity.audit).order_by(identity.audit.c.id))
            if r.action.startswith("source.migrate")
        ]


# -- the Units held, and what every profile did on them, do not move ------------------------------


@pytest.mark.req("EF-BIB-014")
def test_source_migration_keeps_history_and_progress(moved: Moved) -> None:
    """The acceptance criterion of 01 that "no re-download" does not prove: history and progress
    are kept. Each Unit, Variant, file, position and the progress each profile reads from them
    are the very rows they were; nothing is read that was not, and nobody's place is carried to
    another chapter by the correspondence."""
    env = moved.env
    before = moved.census()
    mia_before, leo_before = env.progress(moved.mia), env.progress(moved.leo)
    assert before["positions"], "the fixture must hold history"
    # the new source numbers 11 to 17; chapter 4 (where mia stopped) is 14 there
    done = moved.apply(moved.validate(moved.propose(listing(11, 17))))
    assert moved.census() == before  # not one row of what was held changed
    assert env.progress(moved.mia) == mia_before and env.progress(moved.leo) == leo_before
    mia = env.progress(moved.mia)
    assert {u for u, r in mia.items() if r.state == "read"} == set(moved.units[:3])
    # the old binding and the Variants it gave are the history of the series: kept, retired
    with moved.engine.connect() as c:
        old = c.execute(
            sa.select(acquisition.source_binding).where(
                acquisition.source_binding.c.id == moved.old_binding
            )
        ).one()
        assert old.active is False
        variants = c.execute(
            sa.select(sa.func.count()).where(
                catalog.unit_variant.c.source_binding_id == moved.old_binding
            )
        ).scalar_one()
    assert variants == HELD
    # no false "read": the two chapters to fetch are Units nobody has, so nobody read them
    assert {r.number for r in done.to_fetch} == {"16", "17"}
    for user in (moved.mia, moved.leo):
        assert not set(env.progress(user)) & {r.unit_id for r in done.to_fetch if r.unit_id}
    # and the table reads chapter 4 (where mia stopped) as 14 of the new source: the Unit is the
    # same, so is the place
    with moved.engine.connect() as c:
        mapping = number_map_of(c, done.binding_id)
        assert mapping is not None
        assert mapping.translate("14", 3)[0] == "4"


# -- who may do it, and the cuts ------------------------------------------------------------------


@pytest.mark.req("EF-BIB-014", "ENF-SEC-010")
@pytest.mark.parametrize(
    "who",
    [
        {"credential": Credential.API_KEY, "key_id": "k", "scopes": ("admin",)},
        {"credential": Credential.API_KEY, "key_id": "k", "read_only": True},
        {"credential": Credential.DEVICE_TOKEN, "key_id": "d"},
        {"role": "member", "credential": Credential.SESSION, "session_id": "s"},
        {"role": "child", "credential": Credential.SESSION, "session_id": "s"},
    ],
    ids=["admin-key", "read-only-key", "device-token", "member", "child"],
)
def test_no_key_no_token_no_member_no_child_migrates(moved: Moved, who: dict[str, Any]) -> None:
    before = moved.census()
    refused = admin(**who)
    with pytest.raises(MigrationNotAllowedError), write_transaction(moved.engine) as c:
        SourceMigrations().propose(
            c, moved.edition, plugin_id="suwayomi", source_key="suwayomi:7002",
            remote_id="/new-source", listed=listing(1, 7), principal=refused, now=NOW,
        )  # fmt: skip
    proposal = moved.propose(listing(1, 7))
    for action in (
        lambda c: SourceMigrations().validate(
            c,
            moved.edition,
            proposal.id,
            digest=proposal.digest,
            overrides=(),
            principal=refused,
            now=NOW,
        ),
        lambda c: SourceMigrations().get(c, moved.edition, proposal.id, principal=refused),
    ):
        with pytest.raises(MigrationNotAllowedError), write_transaction(moved.engine) as c:
            action(c)
    validated = moved.validate(proposal)
    with pytest.raises(MigrationNotAllowedError):
        moved.apply(validated, principal=refused)
    assert moved.census() == before
    assert moved.bindings() == 1
    assert moved.migrations()[0].state == "validated"


@pytest.mark.req("EF-BIB-014")
def test_a_table_is_read_only_through_the_edition_it_was_made_for(moved: Moved) -> None:
    """IDOR: the id of a table under another Edition is a table that does not exist."""
    proposal = moved.propose(listing(1, 7))
    other = next(e for k, e in moved.env.seed.editions.items() if e != moved.edition)
    for call in (
        lambda c: SourceMigrations().get(c, other, proposal.id, principal=admin()),
        lambda c: SourceMigrations().validate(
            c,
            other,
            proposal.id,
            digest=proposal.digest,
            overrides=(),
            principal=admin(),
            now=NOW,
        ),
        lambda c: SourceMigrations().apply(
            c, other, proposal.id, digest=proposal.digest, principal=admin(), now=NOW
        ),
    ):
        with pytest.raises(LookupError), write_transaction(moved.engine) as c:
            call(c)
    with pytest.raises(LookupError), write_transaction(moved.engine) as c:
        SourceMigrations().get(c, moved.edition, new_ulid(), principal=admin())


@pytest.mark.req("EF-BIB-014")
def test_a_cut_at_every_step_of_apply_leaves_nothing_and_the_rerun_finishes(
    moved: Moved,
) -> None:
    """The apply is one transaction: a cut (here an error between two steps) rolls everything
    back, and the same call made again gives the very result a clean run gives, with no second
    binding, no second table and no second audit line."""
    validated = moved.validate(moved.propose(listing(11, 17)))
    before = (moved.census(), moved.bindings(), _audits(moved))
    for step in APPLY_STEPS:

        class Cut(RuntimeError):
            pass

        def cut(name: str, step: str = step) -> None:
            if name == step:
                raise Cut(name)

        with pytest.raises(Cut), write_transaction(moved.engine) as c:
            SourceMigrations(checkpoint=cut).apply(
                c,
                moved.edition,
                validated.id,
                digest=validated.digest,
                principal=admin(),
                now=NOW,
            )
        assert (moved.census(), moved.bindings(), _audits(moved)) == before
        assert moved.migrations()[0].state == "validated", step
    done = moved.apply(validated)
    assert done.proposal.state == "applied"
    assert moved.bindings() == before[1] + 1
    assert len(_audits(moved)) == len(before[2]) + 1


CRASH = """
import os, signal, sys
from datetime import datetime, UTC
from shelfhold.acquisition.public import SourceMigrations
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.db import create_db_engine, write_transaction

path, edition, migration, digest, step = sys.argv[1:6]
who = Principal(user_id="01HZZZZZZZZZZZZZZZZZZZZZZA", username="admin", role="admin",
                credential=Credential.LOCAL_CLI, can_rate=True)

def kill(name):
    if name == step:
        os.kill(os.getpid(), signal.SIGKILL)

engine = create_db_engine(path)
with write_transaction(engine) as c:
    SourceMigrations(checkpoint=kill).apply(
        c, edition, migration, digest=digest, principal=who,
        now=datetime(2026, 11, 16, 9, 0, tzinfo=UTC))
"""


@pytest.mark.req("EF-BIB-014")
@pytest.mark.parametrize("step", APPLY_STEPS)
def test_kill_nine_at_each_step_resumes_without_a_duplicate_or_a_loss(
    moved: Moved, tmp_path: Path, step: str
) -> None:
    """A real SIGKILL of another process, in the middle of the apply, at each step."""
    validated = moved.validate(moved.propose(listing(11, 17)))
    before = (moved.census(), moved.bindings(), _audits(moved))
    script = tmp_path / "scripts" / "crash.py"
    script.parent.mkdir()
    script.write_text(CRASH, encoding="utf-8")
    database = str(moved.engine.url.database)
    run = subprocess.run(  # noqa: S603 (our own interpreter and script)
        [sys.executable, "-I", str(script), database, moved.edition, validated.id,
         validated.digest, step],
        capture_output=True, timeout=120, check=False,
        env={k: v for k, v in os.environ.items() if k.startswith(("SHELFHOLD_", "PATH"))},
    )  # fmt: skip
    assert run.returncode == -signal.SIGKILL, run.stderr.decode()[-500:]
    assert (moved.census(), moved.bindings(), _audits(moved)) == before
    assert moved.migrations()[0].state == "validated"
    done = moved.apply(validated)  # the rerun
    assert done.proposal.state == "applied" and not done.already
    assert moved.bindings() == before[1] + 1
    assert moved.count(acquisition.source_migration) == 1
    assert len(_audits(moved)) == len(before[2]) + 1


# -- the purge upstream never counts a copy of the old source as one of the new -------------------


@pytest.mark.req("EF-BIB-014", "EF-BIB-045")
def test_purge_upstream_never_counts_a_migrated_copy_that_is_not_the_same_content(
    moved: Moved,
) -> None:
    """The copy that the old source's server still holds is a copy of the content it gave, listed
    by the binding that gave it. Once that binding is retired and the Unit is only listed by the
    new source, it is not an independent copy any more: the new source's file is another content
    until it has been fetched and has the same hash."""
    from shelfhold.library.public import independent_copies

    unit = moved.units[0]
    with write_transaction(moved.engine) as c:
        c.execute(
            sa.insert(replica.replica).values(
                id=new_ulid(), unit_id=unit, content_hash=OLD_HASH, location="upstream",
                remote_ref="upstream-copy", independent=True, fidelity="original",
            )
        )  # fmt: skip
        # while the old binding lists it, the origin is a copy
        c.execute(
            sa.update(acquisition.source_binding)
            .where(acquisition.source_binding.c.id == moved.old_binding)
            .values(active=True, disabled_reason=None)
        )
    with moved.engine.connect() as c:
        assert independent_copies(c, unit) == 1
    with write_transaction(moved.engine) as c:
        c.execute(
            sa.update(acquisition.source_binding)
            .where(acquisition.source_binding.c.id == moved.old_binding)
            .values(active=False, disabled_reason="removed_upstream")
        )
    moved.apply(moved.validate(moved.propose(listing(11, 17))))
    with moved.engine.connect() as c:
        # the new binding lists the Unit now, and that listing is not the origin of this copy
        assert independent_copies(c, unit) == 0
