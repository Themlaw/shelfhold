"""L25: root takeover|reinit|restored (EF-EXP-064, EF-BIB-054, EF-INT-023)."""

import json
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from shelfhold.export.application.root_exits import (
    RootNotWaitingError,
    RootUnauthenticatedError,
)
from shelfhold.infrastructure.schema import export, platform
from tests.purge_env import ADMIN, KEY, READER
from tests.restore_env import RestoreEnv, restore_env
from tests.root_exits_env import (
    FOREIGN_INSTANCE,
    SENTINEL,
    assert_nothing_lost,
    authentic_kids,
    exits,
    root_id,
    tree,
    write_sentinel,
)


def _roots(env: RestoreEnv) -> int:
    with env.engine.connect() as connection:
        count = connection.execute(sa.select(sa.func.count()).select_from(platform.physical_root))
        return int(count.scalar_one())


@pytest.fixture
def env() -> Iterator[RestoreEnv]:
    with restore_env() as e:
        yield e


def states(watcher: object) -> dict[str, str | None]:
    return {v.subject: v.waiting for v in watcher.refresh()}  # type: ignore[attr-defined]


def record_states(env: RestoreEnv) -> set[str]:
    with env.engine.connect() as connection:
        return set(connection.execute(sa.select(export.export_record.c.state)).scalars())


@pytest.mark.req("EF-EXP-064", "EF-BIB-054")
def test_foreign_root_takeover_keeps_predecessor_kids(env: RestoreEnv) -> None:
    ex, watcher = exits(env, keyring=True)
    rid = root_id(env)
    write_sentinel(
        env.library, instance_id=FOREIGN_INSTANCE, tombstone_kids=authentic_kids(env, rid, "k1")
    )
    assert states(watcher)[rid] == "foreign_root"
    before = tree(env.library)
    report = ex.takeover(rid, principal=ADMIN)
    sentinel = env.sentinel()
    assert sentinel["root_id"] == rid
    assert FOREIGN_INSTANCE in sentinel["predecessor_instance_ids"]
    assert report.predecessors
    assert [k["kid"] for k in sentinel["tombstone_kids"]] == [
        "k1"
    ]  # the authenticated registry is kept
    assert states(watcher)[rid] is None  # the state is left
    assert_nothing_lost(before, env.library)


@pytest.mark.req("EF-EXP-064")
def test_forged_sentinel_cannot_inject_root_id_or_predecessors(env: RestoreEnv) -> None:
    ex, watcher = exits(env)
    rid = root_id(env)
    write_sentinel(
        env.library, instance_id=FOREIGN_INSTANCE, root_id="01J00000000000000000FORGED",
        predecessor_instance_ids=["01J00000000000000000EVIL00"],
        tombstone_kids=[{"kid": "forged", "tags": {"x": "y"}}],
    )  # fmt: skip
    states(watcher)
    ex.takeover(rid, principal=ADMIN)
    sentinel = env.sentinel()
    assert sentinel["root_id"] == rid  # the database's, not the file's
    assert "01J00000000000000000EVIL00" not in sentinel["predecessor_instance_ids"]
    assert all(k["kid"] != "forged" for k in sentinel.get("tombstone_kids", []))


@pytest.mark.req("EF-EXP-064")
def test_root_lost_reinit_marks_missing_never_deletes(env: RestoreEnv) -> None:
    ex, watcher = exits(env)
    rid = root_id(env)
    (env.library / SENTINEL).unlink()
    assert states(watcher)[rid] == "root_lost"
    before = tree(env.library)
    report = ex.reinit(rid, principal=ADMIN)
    assert report.missing > 0
    assert "missing" in record_states(env)
    assert (env.library / SENTINEL).exists()
    assert_nothing_lost(before, env.library)


@pytest.mark.req("EF-EXP-064", "EF-BIB-054")
def test_root_id_distinct_from_instance_id_kept_by_takeover(env: RestoreEnv) -> None:
    ex, watcher = exits(env)
    rid = root_id(env)
    with env.engine.connect() as connection:
        instance = connection.execute(sa.select(platform.instance.c.instance_id)).scalar_one()
    assert rid != instance
    write_sentinel(env.library, instance_id=FOREIGN_INSTANCE)
    states(watcher)
    ex.takeover(rid, principal=ADMIN)
    assert env.sentinel()["root_id"] == rid
    assert env.sentinel()["instance_id"] == instance


@pytest.mark.req("EF-EXP-064", "EF-BIB-054")
def test_takeover_reuses_root_id_from_physical_root(env: RestoreEnv) -> None:
    ex, watcher = exits(env)
    count_before = _roots(env)
    rid = root_id(env)
    (env.library / SENTINEL).unlink()
    states(watcher)
    ex.takeover(str(env.library), principal=ADMIN)  # named by its path
    assert env.sentinel()["root_id"] == rid
    assert _roots(env) == count_before  # no second identity for the same folder


@pytest.mark.req("EF-EXP-064")
def test_root_restored_reexports_missing(env: RestoreEnv) -> None:
    ex, watcher = exits(env)
    rid = root_id(env)
    victim = env.managed()[0]
    victim.unlink()  # the old copy lacked this file
    with env.engine.begin() as connection:
        connection.execute(sa.update(platform.physical_root).values(generation=5))
    write_sentinel(env.library, generation=1)
    assert states(watcher)[rid] == "root_behind"
    report = ex.restored(rid, principal=ADMIN)
    assert report.planned >= 1
    assert env.sentinel()["generation"] == 5
    assert states(watcher)[rid] is None
    env.open_instance()  # the process looks at the disk again
    env.export()  # through the pipeline
    assert victim.exists()


@pytest.mark.req("EF-EXP-064")
def test_restored_never_moves_the_sentinel_backwards(env: RestoreEnv) -> None:
    ex, watcher = exits(env)
    rid = root_id(env)
    with pytest.raises(RootNotWaitingError):
        ex.restored(rid, principal=ADMIN)  # nothing waits: no state is left, nothing written
    assert states(watcher)[rid] is None


@pytest.mark.req("EF-EXP-064")
def test_takeover_refuses_a_root_that_is_fine(env: RestoreEnv) -> None:
    ex, _ = exits(env)
    with pytest.raises(RootNotWaitingError):
        ex.takeover(root_id(env), principal=ADMIN)


@pytest.mark.req("EF-EXP-064", "EF-INT-023")
def test_exits_refuse_an_api_key_and_a_reader(env: RestoreEnv) -> None:
    ex, watcher = exits(env)
    rid = root_id(env)
    (env.library / SENTINEL).unlink()
    states(watcher)
    for principal in (KEY, READER):
        with pytest.raises(PermissionError):
            ex.takeover(rid, principal=principal)
        with pytest.raises(PermissionError):
            ex.reinit(rid, principal=principal)
    assert not (env.library / SENTINEL).exists()  # nothing was written


@pytest.mark.req("EF-EXP-064")
def test_status_lists_the_waiting_root_with_its_command(env: RestoreEnv) -> None:
    _, watcher = exits(env)
    (env.library / SENTINEL).unlink()
    verdicts = [v for v in watcher.refresh() if v.waiting]
    assert [v.waiting for v in verdicts] == ["root_lost"]
    with env.engine.connect() as connection:
        marks = connection.execute(
            sa.text("SELECT key FROM app_setting WHERE key LIKE 'waiting.root_lost.%'")
        ).all()
    assert marks


@pytest.mark.req("EF-EXP-064")
def test_unauthenticated_unknown_folder_is_refused(env: RestoreEnv) -> None:
    ex, _ = exits(env, keyring=True)
    folder = env.library.parent / "elsewhere"
    folder.mkdir()
    (folder / SENTINEL).write_text(
        json.dumps({"root_id": "x" * 26, "instance_id": FOREIGN_INSTANCE, "generation": 1}), "utf-8"
    )
    before = tree(folder)
    report = ex.takeover(str(folder), principal=ADMIN)
    stamped = json.loads((folder / SENTINEL).read_text("utf-8"))
    assert (
        stamped["root_id"] == report.root_id != "x" * 26
    )  # a fresh identity, never the file's claim
    assert "root_id" in report.ignored_claims
    assert set(before) <= set(tree(folder))


@pytest.mark.req("EF-EXP-064")
def test_unknown_folder_without_keyring_is_refused_untouched(env: RestoreEnv) -> None:
    ex, _ = exits(env)  # no keyring to judge the claim with
    folder = env.library.parent / "elsewhere2"
    folder.mkdir()
    forged = json.dumps({"root_id": "y" * 26, "instance_id": FOREIGN_INSTANCE, "generation": 1})
    (folder / SENTINEL).write_text(forged, "utf-8")
    with pytest.raises(RootUnauthenticatedError):
        ex.takeover(str(folder), principal=ADMIN)
    assert (folder / SENTINEL).read_text("utf-8") == forged


@pytest.mark.req("EF-EXP-064", "EF-INT-023")
def test_store_reinit_marks_store_missing(env: RestoreEnv) -> None:
    store = env.library.parent / "kept-store"
    store.mkdir()
    (store / "kept.cbz").write_bytes(b"kept")
    ex, watcher = exits(env, store_path=str(store))
    with env.engine.begin() as connection:
        connection.execute(sa.text("UPDATE unit SET state = 'stored'"))
    assert {v.waiting for v in watcher.refresh() if v.store} <= {"store_lost", None}
    before = tree(store)
    report = ex.store_reinit(principal=ADMIN)
    assert report.units_store_missing > 0
    with env.engine.connect() as connection:
        left = connection.execute(sa.text("SELECT DISTINCT state FROM unit")).scalars().all()
    assert set(left) == {"store_missing"}
    assert (store / SENTINEL).exists()
    assert_nothing_lost(before, store)
    with pytest.raises(PermissionError):
        ex.store_reinit(principal=KEY)


@pytest.mark.req("EF-EXP-064")
def test_exit_during_an_export_takes_the_existing_locks(env: RestoreEnv) -> None:
    from shelfhold.export.application.locks import ResourceLocks
    from shelfhold.export.application.root_exits import RootBusyError
    from shelfhold.infrastructure.ids import new_ulid

    ex, watcher = exits(env)
    rid = root_id(env)
    (env.library / SENTINEL).unlink()
    watcher.refresh()
    other = ResourceLocks(env.engine, clock=env.ex.clock)
    with other.hold_resources(new_ulid(), [f"work:root-{rid}"]):
        with pytest.raises(RootBusyError):
            ex.reinit(rid, principal=ADMIN)
        assert not (env.library / SENTINEL).exists()  # refused before anything was written
    ex.reinit(rid, principal=ADMIN)  # the lock is free again
    assert (env.library / SENTINEL).exists()


@pytest.mark.req("EF-EXP-064", "EF-BIB-054")
def test_takeover_refuses_a_root_id_registered_under_another_path(env: RestoreEnv) -> None:
    """Review: the sentinel of one root copied onto another folder (verified kids and all) never
    lends its `root_id` to that folder."""
    ex, _ = exits(env, keyring=True)
    rid = root_id(env)
    folder = env.library.parent / "copied-sentinel"
    folder.mkdir()
    (folder / SENTINEL).write_text(
        json.dumps({
            "root_id": rid, "instance_id": FOREIGN_INSTANCE, "generation": 1,
            "tombstone_kids": authentic_kids(env, rid, "k1"),
        }),
        "utf-8",
    )  # fmt: skip
    kept = (folder / SENTINEL).read_bytes()
    with pytest.raises(RootUnauthenticatedError):
        ex.takeover(str(folder), principal=ADMIN)
    assert (folder / SENTINEL).read_bytes() == kept
    with env.engine.connect() as connection:
        paths = connection.execute(sa.select(platform.physical_root.c.path_entry)).scalars().all()
    assert str(folder) not in paths


@pytest.mark.req("EF-EXP-064", "EF-BIB-054")
def test_takeover_refuses_verified_kids_when_no_journal_line_opens(env: RestoreEnv) -> None:
    """Review: verified kids are not enough when the folder's journal holds lines that none of
    which opens under the kept `root_id`: their purges would be forgotten."""
    from shelfhold.library.public import RootEvidence

    class Stub:
        def examine(self, _path: str, candidates: object) -> dict[str, RootEvidence]:
            return {c: RootEvidence(c, frozenset({"k1"}), 3, 0) for c in candidates}  # type: ignore[attr-defined]

        def tombstone_kids(self, _path: str) -> dict[str, dict[str, str]]:
            return {}

    ex, _ = exits(env)
    ex._evidence = lambda _fsops: Stub()
    folder = env.library.parent / "journal-unreadable"
    folder.mkdir()
    (folder / SENTINEL).write_text(
        json.dumps({"root_id": "z" * 26, "instance_id": FOREIGN_INSTANCE, "generation": 1}),
        "utf-8",
    )
    with pytest.raises(RootUnauthenticatedError):
        ex.takeover(str(folder), principal=ADMIN)


def marks(env: RestoreEnv) -> dict[str, str]:
    from shelfhold.infrastructure.freeze import FreezeState

    with env.engine.connect() as connection:
        return dict(FreezeState.stored_roots(connection))


@pytest.mark.req("EF-EXP-064", "EF-INT-023")
def test_only_root_lost_is_released_by_the_disk_coming_back(env: RestoreEnv) -> None:
    """Review: a current sentinel copied onto an old copy, or a foreign one put back to the
    expected value, must not lift `root_behind` or `foreign_root`; a vanished sentinel that comes
    back does lift `root_lost`."""
    _ex, watcher = exits(env)
    rid = root_id(env)
    good = (env.library / SENTINEL).read_bytes()
    with env.engine.begin() as connection:
        connection.execute(sa.update(platform.physical_root).values(generation=5))
    write_sentinel(env.library, generation=1)
    assert states(watcher)[rid] == "root_behind"
    (env.library / SENTINEL).write_text(
        json.dumps({**json.loads(good), "generation": 5}), "utf-8"
    )  # the current sentinel, copied onto the old copy
    states(watcher)
    assert marks(env).get(rid) == "root_behind"
    # same for a foreign instance whose sentinel is put back to the expected one
    write_sentinel(env.library, instance_id=FOREIGN_INSTANCE)
    states(watcher)
    assert marks(env).get(rid) == "foreign_root"
    (env.library / SENTINEL).write_bytes(good)
    states(watcher)
    assert marks(env).get(rid) == "foreign_root"  # still waiting for its reserved command
    # a vanished sentinel that comes back by itself is released
    ex, watcher2 = exits(env)
    del ex
    (env.library / SENTINEL).unlink()
    states(watcher2)
    assert marks(env).get(rid) == "root_lost"
    (env.library / SENTINEL).write_bytes(good)
    states(watcher2)
    assert rid not in marks(env)
