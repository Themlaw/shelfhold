"""ENF-SEC-005, EF-BIB-054 (5): tombstones and the keyring (rotation, retired, lost, imported)."""

import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.exc import DatabaseError

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import coherence
from shelfhold.infrastructure.secrets import diagnostics
from shelfhold.infrastructure.secrets.errors import KeyRetirementRefusedError
from shelfhold.infrastructure.secrets.keyring import Keyring, KeyringStore
from shelfhold.infrastructure.secrets.rotation import retire_key, rotate
from shelfhold.library.tombstones import (
    LOST_KEYRING_PHRASE,
    LostKeyringConfirmationError,
)
from tests.intents_env import NOW
from tests.tombstone_env import PLACE, SERIES, VARIANT, TombEnv, make_tomb_env

pytestmark = pytest.mark.req("ENF-SEC-005", "EF-BIB-054")


def _purged(env: TombEnv) -> None:
    service = env.service()
    service.replay(env.roots)
    for values in (VARIANT, PLACE, SERIES):
        service.record(values, env.roots)


def test_lost_keyring_tombstone_journal_inert(tmp_path: Path) -> None:
    """Before the waiver the registered kids block; after `accept-lost-keyring` (typed, audited)
    the lines are inert: they block nothing, delete nothing, `tombstone.unverified`."""
    env = make_tomb_env(tmp_path)
    _purged(env)
    env.store.write(Keyring.generate())  # the keyring is lost: a new one was made
    service = env.service()

    service.replay(env.roots)
    assert service.check(SERIES, root_id=env.library.root_id).cause == "key_unknown"
    assert not service.reconcile_allowed(env.library.root_id)
    assert env.events("tombstone.key_unknown")
    with pytest.raises(LostKeyringConfirmationError):
        service.accept_lost_keyring(env.roots, confirmation="yes", actor_id=None)

    waived = service.accept_lost_keyring(env.roots, confirmation=LOST_KEYRING_PHRASE, actor_id=None)

    assert len(waived) == 1
    for candidate in (VARIANT, PLACE, SERIES):
        assert not service.check(candidate, root_id=env.library.root_id).blocked
    assert service.reconcile_allowed(env.library.root_id)
    assert {i.reason for i in service.report.inert} == {"lost_keyring"}
    assert len(service.report.inert) == 9  # three rows in the table, three lines per root
    assert {e["reason"] for e in env.events("tombstone.unverified")} == {"lost_keyring"}
    with env.engine.begin() as connection, pytest.raises(DatabaseError):  # the waiver is kept
        connection.execute(sa.delete(coherence.tombstone_lost_kid))


def test_unknown_kid_line_blocks_reconcile_until_keyring_import(tmp_path: Path) -> None:
    """A backup of /data older than a rotation (the kid is registered, its key not in the
    keyring): reconcile and reacquisition of the scope are blocked until `secrets import`."""
    env = make_tomb_env(tmp_path)
    before_rotation = env.store.load()
    rotate(env.store, env.engine, now=NOW)
    _purged(env)  # lines under the post-rotation kid
    imported = env.store.load()
    service = env.service(before_rotation)  # the restored copy: keyring of before the rotation

    report = service.replay(env.roots)

    assert {scope for _, scope in report.key_unknown} == {"variant", "place", "series_binding"}
    assert not service.reconcile_allowed(env.library.root_id)
    assert not service.reconcile_allowed(env.books.root_id)
    other = SERIES.__class__(SERIES.source_key, SERIES.sub_source, "/manga/another")
    assert service.check(other, root_id=env.library.root_id).cause == "key_unknown"

    service.use_keyring(imported)  # `secrets import` of the last export
    service.replay(env.roots)

    assert service.reconcile_allowed(env.library.root_id)
    assert service.check(SERIES, root_id=env.library.root_id).cause == "tombstoned"
    assert not service.check(other, root_id=env.library.root_id).blocked


def test_cited_tombstone_kid_never_retired(tmp_path: Path) -> None:
    env = make_tomb_env(tmp_path)
    _purged(env)
    cited = env.store.load().active.kid
    rotate(env.store, env.engine, now=NOW + timedelta(days=400))  # far beyond any retention

    with pytest.raises(KeyRetirementRefusedError) as refused:
        retire_key(env.store, env.engine, cited)

    assert refused.value.reason == "cited_by_tombstone"
    assert env.store.load().find(cited) is not None
    with env.engine.begin() as connection, pytest.raises(DatabaseError):
        connection.execute(
            sa.delete(coherence.secret_key).where(coherence.secret_key.c.kid == cited)
        )


def test_kid_cited_only_by_unmounted_root_never_retired(tmp_path: Path) -> None:
    """The kid of a line that only an unmounted root still holds: the registry still cites it,
    and a registry restored without it learns it again from the sentinel when the root mounts."""
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    service.record(SERIES, [env.books])
    cited = env.store.load().active.kid
    with env.engine.begin() as connection:  # nothing else cites it: no row, no pending line
        connection.execute(sa.delete(coherence.content_tombstone))
        assert connection.execute(sa.select(coherence.tombstone_pending_root)).first() is None
    rotate(env.store, env.engine, now=NOW)

    with pytest.raises(KeyRetirementRefusedError):
        retire_key(env.store, env.engine, cited)

    # A database restored from before the purge (registry empty), the root mounted again: the
    # replay registers the kid from the root's sentinel, and the retirement is still refused.
    restored = make_tomb_env(tmp_path / "restored")
    restored.fs, restored.roots, restored.store = env.fs, env.roots, env.store
    restored.service().replay(env.roots)
    with pytest.raises(KeyRetirementRefusedError):
        retire_key(env.store, restored.engine, cited)


_STEPS = st.lists(st.sampled_from(["purge", "rotate"]), min_size=1, max_size=6)


@settings(max_examples=10)
@given(steps=_STEPS)
def test_rotation_never_rewrites_tombstone_journal(steps: list[str]) -> None:
    """Any interleaving of purges and rotations: the journal only grows, old lines keep their
    kid, new lines take the active one, and every line still opens."""
    with tempfile.TemporaryDirectory() as raw:
        env = make_tomb_env(Path(raw))
        service = env.service()
        service.replay(env.roots)
        previous, kids = b"", []
        for number, step in enumerate(steps):
            if step == "rotate":
                rotate(env.store, env.engine, now=NOW + timedelta(minutes=number))
                service.use_keyring(env.store.load())
            else:
                ref = SERIES.__class__("suwayomi", "1", f"/manga/{number}")
                service.record(ref, env.roots)
                kids.append(env.store.load().active.kid)
            current = env.journal(env.library)
            assert current.startswith(previous)
            previous = current
        lines = previous.splitlines()[1:]
        assert [line.split(b'"kid":"')[1].split(b'"')[0].decode() for line in lines] == kids
        report = env.service().replay(env.roots)
        assert report.inert == [] and report.key_unknown == set()
        assert report.verified == len(kids)


def test_rotation_never_reencrypts_tombstone_table(tmp_path: Path) -> None:
    env = make_tomb_env(tmp_path)
    _purged(env)
    before = env.table_rows()

    rotate(env.store, env.engine, now=NOW)

    assert env.table_rows() == before
    assert len({row["kid"] for row in before}) == 1


def test_status_warns_after_each_rotation_until_export(settings: Settings, tmp_path: Path) -> None:
    """Q64: `status` warns while no export followed the first tombstone, then after each
    rotation until a new export."""
    settings.data_dir.mkdir(parents=True)
    env = make_tomb_env(tmp_path / "instance", database=settings.database_path)
    store = KeyringStore(diagnostics.keyring_path(settings), lock_path=settings.data_dir / ".lock")
    store.create(env.store.load())
    env.store = store

    def codes() -> set[str]:
        return {w.code for w in diagnostics.collect_warnings(settings)}

    store.write(store.load().exported(NOW - timedelta(days=1)))  # exported, then a tombstone
    assert "tombstones_not_exported" not in codes()
    _purged(env)
    assert "tombstones_not_exported" in codes()
    store.write(store.load().exported())
    assert not codes() & {"tombstones_not_exported", "export_older_than_rotation"}
    for _ in range(2):
        rotate(store, env.engine)
        assert "export_older_than_rotation" in codes()
        store.write(store.load().exported())
        assert not codes() & {"tombstones_not_exported", "export_older_than_rotation"}
    store.write(Keyring.generate())  # a keyring without the cited kids
    assert "tombstone_kid_missing" in codes()
