"""M3 L26, second pass of the adversarial review of #233 (after the fixes): proofs of concept, red
against the branch as reviewed. See `docs/decisions/M3.md`, « L26 : contradicteur de la #233
(Opus) », second pass."""

import os
from pathlib import Path

import pytest

from shelfhold.library.tombstones.lift import lift_phrase
from tests.deletion_env import make_deletion
from tests.export_env import export_env
from tests.integration.test_reclaim_l26 import bin_dirs, binned_unit, reclaimer
from tests.intents_env import NOW
from tests.tombstone_env import ADMIN as TOMB_ADMIN
from tests.tombstone_env import VARIANT, make_tomb_env


@pytest.mark.req("EF-BIB-054")
def test_the_lift_of_a_tombstone_from_before_the_identifiers_survives_a_restart(
    tmp_path: Path,
) -> None:
    """A tombstone laid before the event identifiers (every instance that purged before L26 has
    some) is lifted: the lift line gets an `ev` and `lift = "1"` (`LEGACY_TARGET`). The live index
    lets the content go. At the next replay `still_blocking` puts the lift with the events, where
    its target `"1"` names nothing, and the legacy rule only reads lift lines **without** `ev`:
    the old tombstone blocks again, and is listed again, after a lift that was audited as done."""
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    # the line exactly as `record` wrote it before the identifiers: the cited versions, no `ev`
    old = service._record(VARIANT, env.roots, reason=None, cited=service.cited_now(VARIANT))
    restarted = env.service()
    restarted.replay(env.roots)
    assert restarted.check(VARIANT, root_id=None).blocked

    restarted.lift(
        env.roots, seq=old.seq, confirmation=lift_phrase(old.seq), principal=TOMB_ADMIN, now=NOW
    )
    assert not restarted.check(VARIANT, root_id=None).blocked  # the live index: lifted

    again = env.service()  # a restart
    again.replay(env.roots)
    assert not again.check(VARIANT, root_id=env.library.root_id).blocked, (
        "the lift of a tombstone from before the identifiers is undone by the next replay"
    )
    assert old.seq not in {row.seq for row in again.listing()}


@pytest.mark.req("EF-BIB-030")
def test_the_bin_hash_cache_sees_a_file_rewritten_in_place_with_its_mtime_put_back() -> None:
    """The digest cache keys on (path, inode, size, mtime). A file of a bin rewritten in place,
    same length, mtime put back with `utime` (anything with write access to the root can), keeps
    its key: the cache answers the old digest, and `_holds_only_copy` judges bytes that are no
    longer there. `st_ctime_ns`, which `utime` cannot put back, would see it."""
    with export_env() as env:
        d = make_deletion(env)
        binned_unit(d)
        sweeper = reclaimer(d)._holds
        victim = next(
            p
            for entry in bin_dirs(d)
            for p in entry.rglob("*")
            if p.is_file() and p.stat().st_size > 0
        )
        local = str(victim)
        before = sweeper._digest(local)

        seen = victim.stat()
        data = victim.read_bytes()
        rewritten = bytes(b ^ 0xFF for b in data)
        with open(victim, "r+b") as handle:  # same inode, same size
            handle.write(rewritten)
        os.utime(victim, ns=(seen.st_atime_ns, seen.st_mtime_ns))

        after = sweeper._digest(local)
        assert after != before, "the cache returned the digest of bytes that are gone"
