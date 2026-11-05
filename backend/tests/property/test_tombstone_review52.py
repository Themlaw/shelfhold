"""Regressions of the adverse review of PR #52 (L33): each test is one proof of concept, which
asserted the faulty behaviour, turned around."""

import json
import unicodedata
from dataclasses import replace
from pathlib import Path

import nacl.utils
import pytest
import sqlalchemy as sa

from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.schema import coherence, identity, platform
from shelfhold.infrastructure.secrets.errors import KeyRetirementRefusedError
from shelfhold.infrastructure.secrets.rotation import retire_key, rotate
from shelfhold.library.tombstones import (
    LOST_KEYRING_PHRASE,
    PlaceRef,
    SeriesBindingRef,
    TombstoneValueError,
    VariantRef,
)
from shelfhold.library.tombstones.cited import default_normalizers
from shelfhold.library.tombstones.index import MembershipIndex
from shelfhold.library.tombstones.journal import JournalLine, header_bytes
from shelfhold.library.tombstones.service import LostKeyringRefusedError
from tests.intents_env import NOW
from tests.tombstone_env import ADMIN, PLACE, SERIES, SLUG, TITLE, VARIANT, WORK, make_tomb_env

pytestmark = pytest.mark.req("EF-BIB-053", "EF-BIB-054", "ENF-SEC-005")

FORGED = "k-forged-0000"
CITED = {"number": "number@1", "lang": "lang@1", "number_segment": "number_segment@1"}


def _forge_sentinel_kid(root_path: str, entry: object) -> None:
    path = Path(root_path) / ".shelfhold-mount-id"
    doc = json.loads(path.read_text("utf-8"))
    doc["tombstone_kids"] = [*doc.get("tombstone_kids", []), entry]
    path.write_text(json.dumps(doc), "utf-8")


def _forged_line(scope: str, kid: str, seq: int = 1) -> bytes:
    return JournalLine(seq, scope, kid, nacl.utils.random(24), nacl.utils.random(80)).dumps()


@pytest.mark.parametrize("entry", [FORGED, {"kid": FORGED, "tags": {"k-any": "AAAA"}}])
def test_forged_sentinel_kid_is_inert_not_blocking(tmp_path: Path, entry: object) -> None:
    """PoC 1: a third party that writes the sentinel and the journal, without any secret, cannot
    block a scope or the reconcile; it gets an alert (`kid_unregistered`) and nothing else."""
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    _forge_sentinel_kid(env.library.path, entry)
    path = env.journal_path(env.library)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        header_bytes() + _forged_line("variant", FORGED) + _forged_line("series_binding", FORGED)
    )

    report = service.replay(env.roots)

    assert report.key_unknown == set()
    assert {i.reason for i in report.inert} == {"kid_unregistered"}
    other_variant = VariantRef("suwayomi", "1", "/chapter/anything-else")
    other_series = SeriesBindingRef("suwayomi", "1", "/manga/anything-else")
    for root_id in (env.library.root_id, None):
        assert not service.check(other_variant, root_id=root_id).blocked
        assert not service.check(other_series, root_id=root_id).blocked
    assert service.reconcile_allowed(env.library.root_id)
    assert env.events("tombstone.key_unknown") == []
    assert env.events("tombstone.unverified")

    waived = service.accept_lost_keyring(
        env.roots, confirmation=LOST_KEYRING_PHRASE, principal=ADMIN, now=NOW
    )

    assert waived == []
    with env.engine.connect() as connection:
        kids = set(connection.execute(sa.select(coherence.tombstone_kid.c.kid)).scalars())
    assert FORGED not in kids  # an unauthenticated kid of a sentinel is never registered


def test_authenticated_sentinel_entry_blocks_under_a_restored_database(tmp_path: Path) -> None:
    """The legitimate case the sentinel registry exists for: a database restored from before the
    purge, a keyring that lost the newest key but kept an older one. The entry verifies under the
    older key: the scope stays blocked (fail closed) until the key comes back."""
    env = make_tomb_env(tmp_path)
    before = env.store.load()
    rotate(env.store, env.engine, now=NOW)
    service = env.service()
    service.replay(env.roots)
    service.record(SERIES, env.roots)
    restored = make_tomb_env(tmp_path / "restored")
    restored.fs, restored.roots, restored.store = env.fs, env.roots, env.store
    old = restored.service(before)

    report = old.replay(env.roots)

    assert (env.library.root_id, "series_binding") in report.key_unknown
    assert old.check(SERIES, root_id=env.library.root_id).cause == "key_unknown"
    assert not old.check(SERIES, root_id=None).blocked  # no global block: the root decides
    assert not old.reconcile_allowed(env.library.root_id)


def test_key_unknown_on_one_root_does_not_block_another(tmp_path: Path) -> None:
    env = make_tomb_env(tmp_path)
    before = env.store.load()
    rotate(env.store, env.engine, now=NOW)
    service = env.service()
    service.replay(env.roots)
    service.record(SERIES, [env.library])
    restored = make_tomb_env(tmp_path / "restored")
    restored.fs, restored.roots, restored.store = env.fs, env.roots, env.store
    old = restored.service(before)
    old.replay(env.roots)

    other = SeriesBindingRef("suwayomi", "1", "/manga/other")
    assert old.check(other, root_id=env.library.root_id).cause == "key_unknown"
    assert not old.check(other, root_id=env.books.root_id).blocked


def test_cited_kid_of_unmounted_root_never_retired(tmp_path: Path) -> None:
    """PoC 4: the database restored from before the purge does not cite the kid, but a root's
    sentinel does; and a root that is unmounted might: the retirement is refused either way."""
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    service.record(SERIES, [env.library, env.unmounted(env.books)])  # books: never written
    cited = env.store.load().active.kid
    rotate(env.store, env.engine, now=NOW)
    restored = make_tomb_env(tmp_path / "restored")
    with restored.engine.begin() as connection:  # the roots of the instance, not the copy's
        connection.execute(sa.delete(platform.physical_root))
        for root in env.roots:
            connection.execute(
                sa.insert(platform.physical_root).values(root_id=root.root_id, path_entry=root.path)
            )

    with pytest.raises(KeyRetirementRefusedError) as refused:
        retire_key(env.store, restored.engine, cited)
    assert refused.value.reason == "cited_by_tombstone"

    sentinel = Path(env.library.path) / ".shelfhold-mount-id"
    sentinel.rename(sentinel.with_suffix(".away"))  # the only root that cites it is unmounted
    with pytest.raises(KeyRetirementRefusedError) as refused:
        retire_key(env.store, restored.engine, cited)
    assert refused.value.reason == "root_unavailable"
    assert env.store.load().find(cited) is not None


def test_retire_refused_while_any_root_unmounted(tmp_path: Path) -> None:
    env = make_tomb_env(tmp_path)
    retired = env.store.load().active.kid
    rotate(env.store, env.engine, now=NOW)
    sentinel = Path(env.library.path) / ".shelfhold-mount-id"
    sentinel.rename(sentinel.with_suffix(".away"))

    with pytest.raises(KeyRetirementRefusedError) as refused:
        retire_key(env.store, env.engine, retired)

    assert refused.value.reason == "root_unavailable"
    sentinel.with_suffix(".away").rename(sentinel)
    retire_key(env.store, env.engine, retired)  # every root back, nothing cites it
    assert env.store.load().find(retired) is None


def test_place_publisher_compared_in_nfc() -> None:
    """PoC 3 (false negative): the same publisher in NFD is the same Edition."""
    index = MembershipIndex(default_normalizers())
    nfc = PlaceRef.of({"work_id": "W1", "lang": "fr", "publisher": "Glénat"}, track="main",
                      number_segment="", number_raw="7", part="")  # fmt: skip
    index.add_all([(nfc, {**CITED, "publisher": "publisher@1"})])
    nfd = PlaceRef.of(
        {"work_id": "W1", "lang": "fr", "publisher": unicodedata.normalize("NFD", "Glénat")},
        track="main", number_segment="", number_raw="7", part="",
    )  # fmt: skip
    assert nfd.edition_key != nfc.edition_key
    assert index.contains(nfd)


def test_number_segment_derived_again_from_the_raw_number() -> None:
    """PoC 3 (version upgrade): the segment is an output of the number normalizer. A place
    purged while it had none stays blocked when a later version finds one."""
    index = MembershipIndex(default_normalizers())
    purged = PlaceRef.of({"work_id": "W1", "lang": "fr"}, track="main", number_segment="",
                         number_raw="Saison 2 Ch. 7", part="")  # fmt: skip
    index.add_all([(purged, CITED)])
    later = replace(purged, number_segment="S2")
    assert index.contains(later)
    other = PlaceRef.of({"work_id": "W1", "lang": "fr"}, track="main", number_segment="S3",
                        number_raw="Saison 3 Ch. 7", part="")  # fmt: skip
    assert not index.contains(other)


def test_place_names_its_work_and_language() -> None:
    """PoC 3 (false positive): a place without its Work would block chapter N of every Work."""
    for key in ({"lang": "fr"}, {"work_id": WORK}, {"work_id": "", "lang": "fr"}):
        with pytest.raises(TombstoneValueError):
            PlaceRef.of(key, track="main", number_segment="", number_raw="7", part="")
    index = MembershipIndex(default_normalizers())
    index.add_all([(PLACE, CITED)])
    neighbour = PlaceRef.of({**dict(PLACE.edition_key), "work_id": "OTHER"}, track="main",
                            number_segment="", number_raw="7", part="")  # fmt: skip
    assert not index.contains(neighbour)
    assert index.contains(PLACE)


def test_reason_is_an_enumeration(tmp_path: Path) -> None:
    """PoC 6: a free-text reason could carry a title."""
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    with pytest.raises(ValueError, match="reason"):
        service.record(SeriesBindingRef("s", "1", "/x"), env.roots, reason=TITLE[:60])
    for values in (VARIANT, PLACE, SERIES):
        service.record(values, env.roots, reason="purge_unit")
    service.replay(env.roots)
    assert {row["reason"] for row in env.table_rows()} == {"purge_unit"}
    blobs = [env.journal(r) for r in env.roots]
    blobs.extend(p.read_bytes() for p in tmp_path.rglob("*.db*"))
    for blob in blobs:
        assert SLUG.encode() not in blob and TITLE.encode() not in blob


def test_unverified_announced_once_per_line(tmp_path: Path) -> None:
    """PoC 7: a replay at every start must not repeat the alert of the same line."""
    env = make_tomb_env(tmp_path)
    env.fs.fsops.append_tombstone_line(
        env.library.path, _forged_line("variant", "k-x"), header=header_bytes()
    )
    service = env.service()
    for _ in range(5):
        service.replay(env.roots)
    assert len(env.events("tombstone.unverified")) == 1
    env.fs.fsops.append_tombstone_line(
        env.library.path, _forged_line("variant", "k-x", seq=2), header=header_bytes()
    )
    service.replay(env.roots)
    assert len(env.events("tombstone.unverified")) == 2


def test_lost_keyring_waiver_is_reserved_and_audited(tmp_path: Path) -> None:
    """PoC 1 (end): the role is checked in the service, the actor and the kids are audited."""
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    service.record(SERIES, env.roots)
    lost_kid = env.store.load().active.kid
    from shelfhold.infrastructure.secrets.keyring import Keyring

    env.store.write(Keyring.generate())
    lost = env.service()
    lost.replay(env.roots)
    for principal in (
        replace(ADMIN, role="reader"),
        Principal("01J00000000000000000000KEY", "admin", "admin", Credential.API_KEY),
    ):
        with pytest.raises(LostKeyringRefusedError):
            lost.accept_lost_keyring(
                env.roots, confirmation=LOST_KEYRING_PHRASE, principal=principal, now=NOW
            )
    assert lost.check(SERIES, root_id=env.library.root_id).cause == "key_unknown"

    waived = lost.accept_lost_keyring(
        env.roots, confirmation=LOST_KEYRING_PHRASE, principal=ADMIN, now=NOW
    )

    assert waived == [lost_kid]
    with env.engine.connect() as connection:
        audit = connection.execute(sa.select(identity.audit.c.actor_id, identity.audit.c.target))
        rows = audit.all()
    assert rows == [(ADMIN.actor_id, f"kids:{lost_kid}")]
