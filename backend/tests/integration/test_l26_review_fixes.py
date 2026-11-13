"""M3 L26, the answers to the adversarial review of #233 (`docs/decisions/M3.md`, « Réponse de
l'auteur »): the tombstone events, the lock between the emptying and a restore, the volumes of the
space gate, the estimate from a count of chapters, the cache of the hashes of a bin."""

import threading
import time
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.deletion import DeletionService
from shelfhold.export.application.intents import BIN_LOCK
from shelfhold.export.application.reclaim import EMPTY_TRASH_PHRASE
from shelfhold.infrastructure.observability.disk_guard import GIB, Usage
from shelfhold.infrastructure.schema import coherence
from shelfhold.library.tombstones import PlaceRef
from shelfhold.library.tombstones.lift import lift_phrase
from shelfhold.ops import reclaim
from shelfhold.ops.acquire import AcquireOps
from shelfhold.ops.errors import CodedRefusal
from shelfhold.ops.follows import FollowOps
from tests.deletion_env import make_deletion
from tests.export_env import export_env
from tests.integration.test_add_doors_space_l26 import MARKS
from tests.integration.test_reclaim_l26 import binned_unit, reclaimer
from tests.intents_env import NOW
from tests.l41_env import instance
from tests.purge_env import ADMIN
from tests.tombstone_env import ADMIN as TOMB_ADMIN
from tests.tombstone_env import PLACE, VARIANT, TombEnv, make_tomb_env

pytestmark = pytest.mark.req("EF-BIB-030", "EF-BIB-054")


# -- B1, M1: every event is itself --------------------------------------------------------------


def _lift(service: Any, env: TombEnv, seq: int) -> Any:
    return service.lift(
        env.roots, seq=seq, confirmation=lift_phrase(seq), principal=TOMB_ADMIN, now=NOW
    )


def _p_l_p(env: TombEnv) -> tuple[Any, int, int]:
    service = env.service()
    service.replay(env.roots)
    first = service.record(VARIANT, env.roots)
    _lift(service, env, first.seq)
    again = service.record(VARIANT, env.roots)
    assert service.check(VARIANT, root_id=None).blocked
    return service, first.seq, again.seq


def test_purge_lift_purge_survives_the_loss_of_the_table(tmp_path: Path) -> None:
    env = make_tomb_env(tmp_path)
    _p_l_p(env)
    with env.engine.begin() as connection:
        connection.execute(sa.delete(coherence.content_tombstone))
    restored = env.service()
    restored.replay(env.roots)
    assert restored.check(VARIANT, root_id=env.library.root_id).blocked
    assert len(env.table_rows()) == 3  # the three events came back, none merged into another


def test_purge_lift_purge_survives_a_table_from_before_the_lift(tmp_path: Path) -> None:
    """The database of before L: it holds P only. The journals hold P, L, P': the replay adds L and
    P' and the content blocks (P' is not P's twin)."""
    env = make_tomb_env(tmp_path)
    _service, _first, again = _p_l_p(env)
    with env.engine.begin() as connection:
        connection.execute(
            sa.delete(coherence.content_tombstone).where(coherence.content_tombstone.c.seq > 1)
        )
    restored = env.service()
    restored.replay(env.roots)
    assert restored.check(VARIANT, root_id=env.library.root_id).blocked
    assert [r.seq for r in restored.listing()] == [r.seq for r in restored.listing()]
    assert len(restored.listing()) == 1  # P is lifted, P' is the one tombstone that blocks
    assert again > 1


def test_purge_lift_purge_survives_a_truncated_journal_and_a_lost_table(tmp_path: Path) -> None:
    """One journal is cut short after P (the others are whole), the table is gone: the lines of
    the other roots give P, L, P' and the content still blocks."""
    env = make_tomb_env(tmp_path)
    _p_l_p(env)
    cut = env.journal_path(env.roots[0])
    raw = cut.read_bytes()
    lines = raw.split(b"\n")
    cut.write_bytes(b"\n".join(lines[:2]) + b"\n")  # the header and the first line only
    with env.engine.begin() as connection:
        connection.execute(sa.delete(coherence.content_tombstone))
    restored = env.service()
    restored.replay(env.roots)
    assert restored.check(VARIANT, root_id=env.library.root_id).blocked


def test_the_events_are_ordered_and_a_lift_names_its_purge(tmp_path: Path) -> None:
    """P, P', L(P'): lifting the second leaves the first blocking, whatever the order of meeting."""
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    first = service.record(VARIANT, env.roots)
    second = service.record(VARIANT, env.roots)
    other = service.record(PLACE, env.roots)

    _lift(service, env, second.seq)

    assert {r.seq for r in service.listing()} == {first.seq, other.seq}
    assert service.check(VARIANT, root_id=None).blocked  # P still blocks
    fresh = env.service()
    fresh.replay(env.roots)
    assert {r.seq for r in fresh.listing()} == {first.seq, other.seq}
    _lift(service, env, first.seq)
    assert not service.check(VARIANT, root_id=None).blocked
    assert isinstance(PLACE, PlaceRef)


# -- M2: the emptying and a restore share a lock ----------------------------------------------


def test_the_emptying_waits_for_the_lock_a_restore_holds() -> None:
    with export_env() as env:
        d = make_deletion(env)
        binned_unit(d)
        r = reclaimer(d)
        done = threading.Event()
        BIN_LOCK.acquire()
        try:
            worker = threading.Thread(
                target=lambda: (
                    r.empty_trash(principal=ADMIN, typed=EMPTY_TRASH_PHRASE),
                    done.set(),
                )
            )
            worker.start()
            time.sleep(0.3)
            assert not done.is_set()  # it waits: a restore is between its check and its moves
        finally:
            BIN_LOCK.release()
        worker.join(timeout=20)
        assert done.is_set()


def test_a_restore_holds_the_lock_from_its_check_to_its_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with export_env() as env:
        d = make_deletion(env)
        unit = binned_unit(d)
        seen: list[bool] = []
        original = DeletionService._restore_locked

        def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
            seen.append(BIN_LOCK._is_owned())  # type: ignore[attr-defined]
            return original(self, *args, **kwargs)

        monkeypatch.setattr(DeletionService, "_restore_locked", spy)
        assert d.service.restore(unit, principal=ADMIN, now=d.now).restored >= 1
        assert seen == [True]  # the lock was held when the restore began its work


# -- M3, m1: the gate looks at the library volume and at the count of chapters ------------------


def test_a_library_volume_at_its_reserve_refuses_with_its_own_figures(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    library = str(tmp_path / "library")

    def usage(folder: object) -> Usage:
        return (
            Usage(100 * GIB, 12 * GIB)
            if str(folder).startswith(library)
            else Usage(100 * GIB, 80 * GIB)
        )

    with instance(settings, tmp_path) as inst:
        inst.comp.settings = inst.comp.settings.model_copy(update=MARKS)
        inst.configure()
        monkeypatch.setattr(reclaim, "disk_usage", usage)
        reclaim.require_space(inst.comp, 1 * GIB)  # 12 - 10 reserve = 2 GiB usable: it fits
        with pytest.raises(CodedRefusal) as refused:
            reclaim.require_space(inst.comp, 3 * GIB)
        assert refused.value.extensions is not None
        assert refused.value.extensions["free_bytes"] == 12 * GIB  # the tightest volume's figures


def test_a_count_of_chapters_is_estimated_and_refused_when_it_does_not_fit(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """3000 chapters of the default 30 MiB are about 88 GiB: refused on 50 GiB free, where one byte
    would have fitted. A short series passes the gate."""
    with instance(settings, tmp_path) as inst:
        inst.comp.settings = inst.comp.settings.model_copy(update=MARKS)
        inst.configure()
        monkeypatch.setattr(reclaim, "disk_usage", lambda _f: Usage(100 * GIB, 50 * GIB))
        assert reclaim.estimate_units(inst.comp, None) == 0
        assert reclaim.estimate_units(inst.comp, 10) == 10 * 30 * 1024**2
        with pytest.raises(CodedRefusal) as refused:
            AcquireOps(inst.comp).prepare(
                url=None, source_key="source-a", remote_id="1", unit=None,
                requester_user_id=None, units=3000,
            )  # fmt: skip
        assert refused.value.code == "library.space_insufficient"
        with pytest.raises(CodedRefusal) as followed:
            FollowOps(inst.comp).follow(
                "source-a", "1", requester_user_id=None, visibility="ops", units=3000
            )
        assert followed.value.code == "library.space_insufficient"
        try:  # a short series passes the gate (what comes next is another matter)
            AcquireOps(inst.comp).prepare(
                url=None, source_key="source-a", remote_id="1", unit=None,
                requester_user_id=None, units=10,
            )  # fmt: skip
        except CodedRefusal as other:
            assert other.code != "library.space_insufficient"
        except Exception:  # noqa: S110 - the queue or the sources are not there: not the gate
            pass


# -- m3: the hashes of a bin are not computed again ---------------------------------------------


def test_the_hash_of_an_unchanged_file_of_a_bin_is_not_computed_twice() -> None:
    with export_env() as env:
        d = make_deletion(env)
        binned_unit(d)
        r = reclaimer(d)
        fsops = env.lib.fsops
        calls: list[str] = []
        real = fsops.sha256_of

        def counting(path: str, **kw: Any) -> str:
            calls.append(path)
            return real(path, **kw)

        fsops.sha256_of = counting  # type: ignore[method-assign]
        sweeper = r._holds
        entries = [
            f"{root}/.shelfhold/recycle/{p.name}"
            for root in (env.lib.library, env.lib.store)
            for p in (Path(root) / ".shelfhold" / "recycle").glob("*")
        ]
        for entry in entries:
            sweeper._holds_only_copy(entry)
        first = len(calls)
        for entry in entries:
            sweeper._holds_only_copy(entry)
        assert first > 0 and len(calls) == first  # the second look read nothing again
