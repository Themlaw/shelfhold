"""M3 L26, the adversarial review of #233: proofs of concept, red against the branch as reviewed.

Each test states an invariant of the card (EF-BIB-030, EF-BIB-052, EF-BIB-054) and tries to break
it the way an interleaving, a restore or a second volume would. They are expected to FAIL until the
objection they carry (`docs/decisions/M3.md`, « L26 : contradicteur de la #233 (Opus) ») is fixed
or rejected in writing.
"""

from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.reclaim import EMPTY_TRASH_PHRASE
from shelfhold.infrastructure.observability.disk_guard import GIB, Usage
from shelfhold.infrastructure.schema import coherence
from shelfhold.library.tombstones.lift import lift_phrase
from shelfhold.ops import reclaim
from shelfhold.ops.errors import CodedRefusal
from tests.deletion_env import make_deletion
from tests.export_env import export_env
from tests.integration.test_reclaim_l26 import bin_dirs, binned_unit, reclaimer
from tests.intents_env import NOW
from tests.l41_env import instance
from tests.purge_env import ADMIN
from tests.tombstone_env import ADMIN as TOMB_ADMIN
from tests.tombstone_env import VARIANT, make_tomb_env

MARKS = {
    "disk_pause_percent": 5.0, "disk_pause_gib": 10.0,
    "disk_resume_percent": 8.0, "disk_resume_gib": 15.0,
}  # fmt: skip


# -- B1: a purge laid after a lift is lost by the replay of a restored database ----------------


@pytest.mark.req("EF-BIB-054")
def test_a_purge_after_a_lift_still_blocks_after_a_restore_of_the_database(
    tmp_path: Path,
) -> None:
    """Purge P, lift L, purge again P' (same values: the same content, purged once more). The
    database is restored from before all three (or lost): only the journals are left. The replay
    must give back P, L, P' in that order, so that P' blocks. It dedups the lines by identity
    (`_identity(values, cited)`): P' is P's twin and is dropped, the index ends at « lifted », and
    the purged content can be acquired again (EF-BIB-052 (3): a `restored_behind` database never
    brings a purged content back)."""
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    first = service.record(VARIANT, env.roots)
    service.lift(
        env.roots, seq=first.seq, confirmation=lift_phrase(first.seq), principal=TOMB_ADMIN,
        now=NOW,
    )  # fmt: skip
    service.record(VARIANT, env.roots)  # purged again after the lift: it blocks again
    assert service.check(VARIANT, root_id=None).blocked

    with env.engine.begin() as connection:  # the database of before: the journals survive
        connection.execute(sa.delete(coherence.content_tombstone))
    restored = env.service()
    restored.replay(env.roots)

    assert restored.check(VARIANT, root_id=env.library.root_id).blocked, (
        "the second purge was dropped by the replay: the content is acquirable again"
    )


# -- M1: a lift of one sequence lifts its twins too ----------------------------------------------


@pytest.mark.req("EF-BIB-054")
def test_a_lift_names_one_sequence_and_lifts_that_one_only(tmp_path: Path) -> None:
    """Two tombstones of the same values (two purges that laid the same line) are two sequences
    in `tombstone list`. Lifting the first must leave the second listed and blocking: the typed
    phrase `lift <seq>` names one tombstone. `_active` and `MembershipIndex.remove` take out every
    entry equal to it, so the second vanishes from the list, unaudited."""
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    first = service.record(VARIANT, env.roots)
    second = service.record(VARIANT, env.roots)

    service.lift(
        env.roots, seq=first.seq, confirmation=lift_phrase(first.seq), principal=TOMB_ADMIN,
        now=NOW,
    )  # fmt: skip

    listed = {row.seq for row in service.listing()}
    assert second.seq in listed, "the lift of one sequence took another one out of the list"


# -- M2: an intent opened between the hold check and the removal is not seen --------------------


@pytest.mark.req("EF-BIB-030")
def test_empty_trash_never_removes_an_entry_an_intent_opened_after_the_hold_check() -> None:
    """`empty_trash` asks `holds()` (open intents included), then removes, without any lock that a
    restore takes (the restore holds the Work lock, the reclaim only `_RUN_LOCK`) and without the
    removal asking the journal again. A restore that commits its `unbin` intents in between (its
    transaction records them before it moves a file) sees its files removed under it: the rows of
    the Unit are back, the bytes are gone. Simulated by opening the restore's intent right after
    the check, exactly where the interleaving falls."""
    with export_env() as env:
        d = make_deletion(env)
        binned_unit(d)
        r = reclaimer(d)
        journal = IntentJournal(env.engine, env.lib.fsops, clock=env.clock)
        opened: list[str] = []
        check = r._holds.holds

        def holds_then_a_restore_commits(name: str, entry: str) -> str | None:
            verdict = check(name, entry)
            intent = journal.record(op="unbin", src="x", dst=f"{name}/x")  # the restore's tx
            journal.mark_applying(intent)
            opened.append(name)
            return verdict

        r._holds.holds = holds_then_a_restore_commits  # type: ignore[method-assign]
        r.empty_trash(principal=ADMIN, typed=EMPTY_TRASH_PHRASE)

        assert opened, "the hold check ran"
        left = {p.name for p in bin_dirs(d)}
        assert set(opened) <= left, "an entry a restore was using was removed under it"


# -- M3: the space gate does not look at the volume of the library roots -----------------------


@pytest.mark.req("EF-BIB-030")
def test_an_addition_is_refused_when_the_library_volume_is_at_its_reserve(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """An addition writes into /store, /data **and** the destination of its shelf (a library root,
    often another disk, the one the disk guard watches as `library`). `/data` and `/store` have
    room, the library volume is at 2 GiB of 100 (under its 10 GiB reserve): the gate must refuse.
    `space_check` measures `store_dir` and `data_dir` only, so it lets the addition through."""
    library_volume = str(tmp_path / "library")

    def usage(folder: object) -> Usage:
        if str(folder).startswith(library_volume):
            return Usage(100 * GIB, 2 * GIB)
        return Usage(100 * GIB, 50 * GIB)

    with instance(settings, tmp_path) as inst:
        inst.comp.settings = inst.comp.settings.model_copy(update=MARKS)
        inst.configure()  # a Kavita target whose rw root is under tmp/library
        monkeypatch.setattr(reclaim, "disk_usage", usage)

        with pytest.raises(CodedRefusal) as refused:
            reclaim.require_space(inst.comp, 1 * GIB)
        assert refused.value.code == "library.space_insufficient"
