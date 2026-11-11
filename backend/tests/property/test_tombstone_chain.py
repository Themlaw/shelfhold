"""EF-BIB-054 (6), (10), EF-INT-023, ENF-OPS-011 (M1b L26): the chained tombstone journal, the
detection of its truncation, and `tombstone rekey`.

The last `seq` and the chained label of each root are kept in the database and in the sentinel
(authenticated under the keyring); a journal that stops before them was truncated. The database is
the authority: a sentinel rewritten by a third party proves nothing. Reordering and duplicating
lines is harmless. `rekey` rewrites a journal under the active key through a new file and an atomic
`rename`; nothing is lost, whatever stops it.
"""

import dataclasses
import json
import os
import signal
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from hypothesis import given, settings
from hypothesis import strategies as st

from shelfhold.export.application.intents import INTENT_SCHEMA_VERSION, IntentJournal
from shelfhold.identity.public import RESERVED_ACTIONS, Credential
from shelfhold.infrastructure.fsops.confined import ConfinedRoot
from shelfhold.infrastructure.schema import coherence, identity, platform
from shelfhold.infrastructure.secrets import rotation
from shelfhold.infrastructure.secrets.errors import DecryptionError
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.rotation import rotate
from shelfhold.infrastructure.secrets.tombstone_aead import open_values
from shelfhold.library.tombstones import (
    SeriesBindingRef,
    TombstoneRoot,
    TombstoneService,
)
from shelfhold.library.tombstones.journal import parse_journal
from shelfhold.library.tombstones.service import LOST_KEYRING_PHRASE
from shelfhold.library.tombstones.state import INTENT_SCHEMA_VERSION as STATE_INTENT_VERSION
from shelfhold.library.tombstones.values import TombstoneValues, decode_plaintext
from tests.intents_env import NOW
from tests.tombstone_env import ADMIN, PLACE, SERIES, VARIANT, TombEnv, make_tomb_env

pytestmark = pytest.mark.req("EF-BIB-054")

CHAIN_FIELD = "tombstone_chain"  # the authenticated copy in the sentinel


def _purged(env: TombEnv) -> TombstoneService:
    service = env.service()
    service.replay(env.roots)
    for values in (VARIANT, PLACE, SERIES):
        service.record(values, env.roots)
    return service


def _cut_last_line(env: TombEnv, root: TombstoneRoot) -> None:
    path = env.journal_path(root)
    rows = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(rows[:-1]))


def _edit_sentinel(
    env: TombEnv, root: TombstoneRoot, edit: Callable[[dict[str, Any]], None]
) -> None:
    path = Path(root.path) / ".shelfhold-mount-id"
    document = json.loads(path.read_text("utf-8"))
    edit(document)
    path.write_text(json.dumps(document), "utf-8")


def _forget_database_chain(env: TombEnv) -> None:
    """A database restored from before the purges: what it knew of the journals is gone."""
    with env.engine.begin() as connection:
        connection.execute(
            sa.delete(platform.app_setting).where(platform.app_setting.c.key.like("%tombstone%"))
        )


def _tombstones(
    env: TombEnv, root: TombstoneRoot, keyring: Keyring
) -> set[tuple[TombstoneValues, tuple[tuple[str, str], ...]]]:
    """What the journal of a root says, opened with the keyring: the set of tombstones."""
    journal = parse_journal(env.journal_path(root).read_bytes())
    found = set()
    for line in journal.lines:
        key = keyring.get(line.kid)
        plaintext = open_values(
            key.material, line.nonce, line.ct, scope=line.scope, root=root.root_id, seq=line.seq
        )
        values, cited = decode_plaintext(plaintext, line.scope)
        found.add((values, tuple(sorted(cited.items()))))
    return found


# -- truncation -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tamper",
    [
        "sentinel untouched",
        "sentinel chain removed",
        "sentinel chain rewritten to the short journal",
    ],
)
def test_tombstone_journal_truncation_detected(tmp_path: Path, tamper: str) -> None:
    """A line cut off the end of a journal is noticed, even when the attacker rewrites the
    sentinel to match: the database is the authority. Only that root is concerned."""
    env = make_tomb_env(tmp_path)
    _purged(env)
    _cut_last_line(env, env.library)
    if tamper == "sentinel chain removed":
        _edit_sentinel(env, env.library, lambda doc: doc.pop(CHAIN_FIELD, None))
    elif tamper == "sentinel chain rewritten to the short journal":

        def forge(doc: dict[str, Any]) -> None:
            doc[CHAIN_FIELD] = {"seq": 2, "count": 2, "label": "A" * 43, "tags": {"k-0": "B" * 43}}

        _edit_sentinel(env, env.library, forge)
    service = env.service()

    report = service.replay(env.roots)

    assert report.truncated_roots == {env.library.root_id}
    assert [e["root_id"] for e in env.events("tombstone.journal_truncated")] == [
        env.library.root_id
    ]
    assert not service.reconcile_allowed(env.library.root_id)
    assert service.reconcile_allowed(env.books.root_id)  # this root only (D12)


def test_truncation_detected_from_the_sentinel_when_the_database_is_behind(tmp_path: Path) -> None:
    """The database comes from a backup older than the purges, the attacker cut the journal: the
    authenticated copy in the sentinel is what remembers (EF-BIB-054 (6))."""
    env = make_tomb_env(tmp_path)
    _purged(env)
    assert CHAIN_FIELD in env.sentinel(env.library), "the sentinel keeps the chained label"
    _cut_last_line(env, env.library)
    _forget_database_chain(env)

    report = env.service().replay(env.roots)

    assert env.library.root_id in report.truncated_roots


def test_a_journal_older_than_the_chain_is_not_truncated(tmp_path: Path) -> None:
    """Nothing is cut: lines added since, a second replay, a restart; no alert, no block."""
    env = make_tomb_env(tmp_path)
    _purged(env)
    for _ in range(2):
        report = env.service().replay(env.roots)
        assert report.truncated_roots == set()
    assert env.events("tombstone.journal_truncated") == []


def test_legacy_journal_without_chain_is_baselined_not_flagged(tmp_path: Path) -> None:
    """A journal written before the chain existed (M1a) is taken as it is at the first replay;
    it is protected from then on."""
    env = make_tomb_env(tmp_path)
    _purged(env)
    _forget_database_chain(env)

    def as_m1a(doc: dict[str, Any]) -> None:
        doc.pop(CHAIN_FIELD, None)
        doc["schema_version"] = "1.1"  # the minor version before the chain

    _edit_sentinel(env, env.library, as_m1a)
    assert env.service().replay(env.roots).truncated_roots == set()

    _cut_last_line(env, env.library)  # now it is tracked
    assert env.service().replay(env.roots).truncated_roots == {env.library.root_id}


def test_tombstone_journal_reorder_and_duplicate_harmless(tmp_path: Path) -> None:
    """Lines shuffled or repeated change nothing: no alert, nothing inert, the same tombstones."""
    env = make_tomb_env(tmp_path)
    _purged(env)
    path = env.journal_path(env.library)
    header, *rows = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(header + b"".join([rows[2], rows[0], rows[1], rows[0], rows[2], rows[2]]))
    service = env.service()

    report = service.replay(env.roots)

    assert report.truncated_roots == set()
    assert env.events("tombstone.journal_truncated") == []
    assert [i for i in report.inert if i.root_id == env.library.root_id] == []
    assert service.reconcile_allowed(env.library.root_id)
    assert service.check(SERIES, root_id=env.library.root_id).cause == "tombstoned"
    assert len(env.table_rows()) == 3  # nothing was merged twice


def test_truncated_journal_blocks_reconcile_until_exit(tmp_path: Path) -> None:
    """The root is blocked (reconcile, reacquisition) and says so in a waiting mark; the exit is
    `tombstone set-aside-journal`, which writes the journal again from the table (D12). The
    old file is kept, no tombstone is lost."""
    env = make_tomb_env(tmp_path)
    _purged(env)
    _cut_last_line(env, env.library)
    service = env.service()
    service.replay(env.roots)

    other = SeriesBindingRef("suwayomi", "9", "/manga/another")
    assert service.check(other, root_id=env.library.root_id).cause == "journal_truncated"
    assert not service.check(other, root_id=env.books.root_id).blocked
    with env.engine.connect() as connection:
        marks = (
            connection.execute(
                sa.select(platform.app_setting.c.key).where(
                    platform.app_setting.c.key.like("waiting.tombstone_journal_truncated.%")
                )
            )
            .scalars()
            .all()
        )
    assert marks == [f"waiting.tombstone_journal_truncated.{env.library.root_id}"]

    done = service.set_aside_journal(env.roots, principal=ADMIN, now=NOW, root_id=None)

    assert set(done) == {env.library.root_id}
    assert Path(env.library.path, done[env.library.root_id]).exists()  # kept, never deleted
    assert service.reconcile_allowed(env.library.root_id)
    assert not service.check(other, root_id=env.library.root_id).blocked
    keyring = env.store.load()
    assert _tombstones(env, env.library, keyring) == _tombstones(env, env.books, keyring)
    assert len(_tombstones(env, env.library, keyring)) == 3  # the cut line is back
    again = env.service().replay(env.roots)
    assert again.truncated_roots == set()
    with env.engine.connect() as connection:  # the mark went with the exit
        left = connection.execute(
            sa.select(sa.func.count()).where(
                platform.app_setting.c.key.like("waiting.tombstone_journal_truncated.%")
            )
        ).scalar()
    assert left == 0


def test_set_aside_of_a_journal_deleted_whole_writes_it_again(tmp_path: Path) -> None:
    env = make_tomb_env(tmp_path)
    _purged(env)
    env.journal_path(env.library).unlink()
    service = env.service()
    assert env.library.root_id in service.replay(env.roots).truncated_roots

    done = service.set_aside_journal(env.roots, principal=ADMIN, now=NOW)

    assert done == {}  # nothing to put aside
    assert len(_tombstones(env, env.library, env.store.load())) == 3
    assert env.service().replay(env.roots).truncated_roots == set()


# -- rekey ------------------------------------------------------------------------------------


def _leak_and_rotate(env: TombEnv) -> str:
    """The active key leaks: a new one is issued (`secrets rotate`), the old one is marked
    compromised. Returns the old kid."""
    old = env.store.load().active.kid
    rotate(env.store, env.engine, now=NOW)
    rotation.mark_compromised(env.store, env.engine, [old], now=NOW)
    return old


def _rekeyed(env: TombEnv, old: str) -> TombstoneService:
    service = env.service(env.store.load())
    service.replay(env.roots)
    report = service.rekey(env.roots, principal=ADMIN, now=NOW)
    assert report.rewritten == {r.root_id: 3 for r in env.roots}
    return service


def test_tombstone_rekey_after_leak_rewrites_journal_atomically(tmp_path: Path) -> None:
    """Every line is under the new key, the old key opens none of them, the old kid is
    compromised and kept, and no tombstone is lost. A kill never leaves a journal empty or
    half written."""
    env = make_tomb_env(tmp_path)
    _purged(env)
    old_keyring = env.store.load()
    before = _tombstones(env, env.library, old_keyring)
    old = _leak_and_rotate(env)
    keyring = env.store.load()

    service = _rekeyed(env, old)

    assert keyring.find(old) is not None and keyring.find(old).state == "compromised"  # type: ignore[union-attr]
    with env.engine.connect() as connection:
        state = connection.execute(
            sa.select(coherence.secret_key.c.state).where(coherence.secret_key.c.kid == old)
        ).scalar()
        actions = connection.execute(sa.select(identity.audit.c.action)).scalars().all()
    assert state == "compromised"
    assert "tombstone.rekey" in actions
    for root in env.roots:
        journal = parse_journal(env.journal_path(root).read_bytes())
        assert {line.kid for line in journal.lines} == {keyring.active.kid}
        assert _tombstones(env, root, keyring) == before
        for line in journal.lines:  # the old key can no longer open the new journal
            with pytest.raises(DecryptionError):
                open_values(
                    old_keyring.get(old).material,
                    line.nonce,
                    line.ct,
                    scope=line.scope,
                    root=root.root_id,
                    seq=line.seq,
                )
    assert {row["kid"] for row in env.table_rows()} == {keyring.active.kid}
    assert service.check(SERIES, root_id=env.library.root_id).cause == "tombstoned"
    after = env.service(keyring).replay(env.roots)  # nothing looks truncated or forged
    assert after.truncated_roots == set() and after.inert == []


def test_the_intent_version_written_by_rekey_is_the_journal_one() -> None:
    assert STATE_INTENT_VERSION == INTENT_SCHEMA_VERSION


def test_rekey_is_declared_as_a_reserved_action() -> None:
    assert "tombstone.rekey" in RESERVED_ACTIONS


def test_rekey_refuses_the_active_key(tmp_path: Path) -> None:
    env = make_tomb_env(tmp_path)
    _purged(env)
    with pytest.raises(ValueError, match="active"):
        rotation.mark_compromised(env.store, env.engine, [env.store.load().active.kid], now=NOW)


@pytest.mark.parametrize("point", ["before_rename", "after_rename"])
def test_rekey_killed_midway_never_loses_the_journal(tmp_path: Path, point: str) -> None:
    """A SIGKILL before or after the `rename`: the journal is whole (old or new), every
    tombstone opens with the keyring, no alert, and running the command again finishes."""
    env = make_tomb_env(tmp_path)
    _purged(env)
    before = _tombstones(env, env.library, env.store.load())
    _leak_and_rotate(env)
    keyring = env.store.load()
    service = env.service(keyring)
    service.replay(env.roots)
    env.engine.dispose()
    real_rename = ConfinedRoot.rename

    def dying(self: ConfinedRoot, src: str, dst: str, **kwargs: Any) -> None:
        if dst.endswith(".shelfhold/tombstones"):
            if point == "before_rename":
                os.kill(os.getpid(), signal.SIGKILL)
            real_rename(self, src, dst, **kwargs)
            os.kill(os.getpid(), signal.SIGKILL)
        real_rename(self, src, dst, **kwargs)

    child = os.fork()
    if child == 0:  # pragma: no cover (dies by SIGKILL)
        try:
            ConfinedRoot.rename = dying  # type: ignore[method-assign]
            service.rekey(env.roots, principal=ADMIN, now=NOW)
        finally:
            os._exit(0)
    _, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    recovery = IntentJournal(env.engine, env.fs.fsops)  # the start-up that follows the kill
    assert [i.op for i in recovery.open_intents()] == ["tombstone_rekey"]
    resolved = recovery.resolve_open()
    assert [*resolved.refused, *resolved.drifted] == [] and len(resolved.completed) == 1

    for root in env.roots[:1]:
        raw = env.journal_path(root).read_bytes()
        assert raw, "never an empty journal"
        assert _tombstones(env, root, keyring) == before  # nothing lost, old or new
    fresh = env.service(keyring)
    report = fresh.replay(env.roots)  # the restart: no false alarm
    assert report.truncated_roots == set()
    assert fresh.rekey(env.roots, principal=ADMIN, now=NOW).rewritten.keys() <= {
        r.root_id for r in env.roots
    }
    for root in env.roots:
        journal = parse_journal(env.journal_path(root).read_bytes())
        assert {line.kid for line in journal.lines} == {keyring.active.kid}
        assert _tombstones(env, root, keyring) == before
    assert env.service(keyring).replay(env.roots).truncated_roots == set()


def test_rekey_of_unmounted_root_at_remount(tmp_path: Path) -> None:
    """A root that is away keeps its journal for now, no other write is accepted on it, and it is
    rewritten when it comes back, before anything else."""
    env = make_tomb_env(tmp_path)
    _purged(env)
    away = env.unmounted(env.books)
    untouched = env.journal_path(env.books).read_bytes()
    old = _leak_and_rotate(env)
    keyring = env.store.load()
    service = env.service(keyring)
    service.replay(env.roots)

    report = service.rekey([env.library, away], principal=ADMIN, now=NOW)

    assert report.rewritten == {env.library.root_id: 3}
    assert report.owed == (env.books.root_id,)
    assert env.journal_path(env.books).read_bytes() == untouched
    assert service.has_pending(env.books.root_id)  # fsops refuses every other write to it

    remounted = env.service(keyring)
    replay = remounted.replay(env.roots)

    assert not remounted.has_pending(env.books.root_id)
    journal = parse_journal(env.journal_path(env.books).read_bytes())
    assert {line.kid for line in journal.lines} == {keyring.active.kid}
    assert old not in {line.kid for line in journal.lines}
    assert replay.truncated_roots == set()
    assert _tombstones(env, env.books, keyring) == _tombstones(env, env.library, keyring)


def test_tombstone_added_during_rekey_is_not_lost(tmp_path: Path) -> None:
    """A purge that records a tombstone while a journal is being rewritten: its line lands in
    the new file (the rewrite and the append never interleave), and it is in the table."""
    env = make_tomb_env(tmp_path)
    _purged(env)
    _leak_and_rotate(env)
    keyring = env.store.load()
    rekeying = env.service(keyring)
    rekeying.replay(env.roots)
    recording = env.service(keyring)
    recording.replay(env.roots)
    extra = SeriesBindingRef("suwayomi", "7777", "/manga/added-during-rekey")
    inside, finished = threading.Event(), threading.Event()
    real_rename = ConfinedRoot.rename

    def slow(self: ConfinedRoot, src: str, dst: str, **kwargs: Any) -> None:
        if dst.endswith(".shelfhold/tombstones"):
            inside.set()
            finished.wait(timeout=0.5)  # the purge tries to append during this window
        real_rename(self, src, dst, **kwargs)

    def purge() -> None:
        inside.wait(timeout=10)
        recording.record(extra, env.roots)

    thread = threading.Thread(target=purge)
    thread.start()
    ConfinedRoot.rename = slow  # type: ignore[method-assign]
    try:
        rekeying.rekey(env.roots, principal=ADMIN, now=NOW)
    finally:
        ConfinedRoot.rename = real_rename  # type: ignore[method-assign]
        finished.set()
    thread.join(timeout=30)
    assert not thread.is_alive()

    for root in env.roots:
        journal = {(v, c) for v, c in _tombstones(env, root, keyring)}
        assert any(values == extra for values, _ in journal), "the line added meanwhile is there"
        assert len(journal) == 4
    assert len(env.table_rows()) == 4
    assert env.service(keyring).replay(env.roots).truncated_roots == set()


@settings(max_examples=6, deadline=None)
@given(numbers=st.lists(st.integers(1, 40), min_size=1, max_size=5, unique=True))
def test_rekey_then_read_gives_the_same_tombstones(numbers: list[int]) -> None:
    """Property: rekey then read yields the same set of tombstones, in every root and the table."""
    with tempfile.TemporaryDirectory(prefix="sh-rk-") as raw:
        env = make_tomb_env(Path(raw))
        service = env.service()
        service.replay(env.roots)
        for n in numbers:
            service.record(SeriesBindingRef("suwayomi", str(n), f"/manga/s-{n}"), env.roots)
        before = {r.root_id: _tombstones(env, r, env.store.load()) for r in env.roots}
        _leak_and_rotate(env)
        keyring = env.store.load()
        rekeying = env.service(keyring)
        rekeying.replay(env.roots)
        rekeying.rekey(env.roots, principal=ADMIN, now=NOW)
        for root in env.roots:
            assert _tombstones(env, root, keyring) == before[root.root_id]
            kids = {ln.kid for ln in parse_journal(env.journal_path(root).read_bytes()).lines}
            assert kids == {keyring.active.kid}
        assert env.service(keyring).replay(env.roots).truncated_roots == set()


def test_rekey_is_refused_to_a_key(tmp_path: Path) -> None:
    """Reserved (EF-USR-024): never a key, whatever its role."""
    env = make_tomb_env(tmp_path)
    _purged(env)
    old = _leak_and_rotate(env)
    service = env.service(env.store.load())
    service.replay(env.roots)
    api_key = dataclasses.replace(ADMIN, credential=Credential.API_KEY)

    with pytest.raises(PermissionError):
        service.rekey(env.roots, principal=api_key, now=NOW)

    assert {r["kid"] for r in env.table_rows()} == {old}  # nothing was touched


# -- review of #121 -----------------------------------------------------------------------


def _chain_rows(env: TombEnv) -> dict[str, str]:
    with env.engine.connect() as connection:
        return dict(
            connection.execute(
                sa.select(platform.app_setting.c.key, platform.app_setting.c.value).where(
                    platform.app_setting.c.key.like("tombstone.chain.%")
                )
            ).all()
        )


@pytest.mark.parametrize("strip", ["field removed", "mac replaced by noise", "version lowered too"])
def test_cut_after_a_restore_is_seen_even_if_the_sentinel_chain_is_stripped(
    tmp_path: Path, strip: str
) -> None:
    """/data is restored to before the last purge, the journal is cut back to it, and the
    sentinel's chain is removed or broken (no MAC to forge): the database tracks the journal and
    the sentinel has carried a chain, so a chain that does not verify is a cut, not a new root."""
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    service.record(VARIANT, env.roots)
    old = _chain_rows(env)  # the backup
    service.record(PLACE, env.roots)
    for root in env.roots:
        _cut_last_line(env, root)
    with env.engine.begin() as connection:  # the restore: the database as the backup had it
        for key, value in old.items():
            connection.execute(
                sa.update(platform.app_setting)
                .where(platform.app_setting.c.key == key)
                .values(value=value)
            )

    def strip_it(doc: dict[str, Any]) -> None:
        if strip == "mac replaced by noise":
            doc[CHAIN_FIELD]["tags"] = {k: "AAAA" for k in doc[CHAIN_FIELD]["tags"]}
            return
        doc.pop(CHAIN_FIELD, None)
        if strip == "version lowered too":
            doc["schema_version"] = "1.1"

    for root in env.roots:
        _edit_sentinel(env, root, strip_it)

    report = env.service().replay(env.roots)

    assert report.truncated_roots == {r.root_id for r in env.roots}


def test_sentinel_that_has_written_a_chain_and_shows_none_is_suspect(tmp_path: Path) -> None:
    """The version marks a sentinel that carried a chain: with the database empty-handed too (a
    reinstalled instance), a stripped field is not a root that is simply new."""
    env = make_tomb_env(tmp_path)
    _purged(env)
    _forget_database_chain(env)
    _edit_sentinel(env, env.library, lambda doc: doc.pop(CHAIN_FIELD, None))

    assert env.service().replay(env.roots).truncated_roots == {env.library.root_id}


def test_lines_of_a_lost_key_still_count_in_the_chain(tmp_path: Path) -> None:
    """`accept-lost-keyring` waives the lines that cannot be opened, not the journal: their tag
    needs no key, so the cut of a later line is still seen and an untouched journal is whole."""
    env = make_tomb_env(tmp_path)
    _purged(env)
    env.store.write(Keyring.generate())  # the keyring is lost
    service = env.service()
    service.replay(env.roots)
    service.accept_lost_keyring(
        env.roots, confirmation=LOST_KEYRING_PHRASE, principal=ADMIN, now=NOW
    )
    service.record(VARIANT, env.roots)  # a line under the new key, after the waiver
    assert env.service().replay(env.roots).truncated_roots == set()

    _cut_last_line(env, env.library)

    assert env.service().replay(env.roots).truncated_roots == {env.library.root_id}
