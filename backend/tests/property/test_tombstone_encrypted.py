"""EF-BIB-054 (1), I46: nothing in a tombstone is readable without the keyring; a line is bound to
its root, its scope and its seq."""

import base64
import hashlib
import hmac
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import pytest
import sqlalchemy as sa
from hypothesis import given, settings
from hypothesis import strategies as st

from shelfhold.infrastructure.schema import coherence, identity, platform
from shelfhold.infrastructure.secrets.errors import DecryptionError
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.tombstone_aead import hkdf_sha256, open_values
from shelfhold.library.tombstones import (
    LOST_KEYRING_PHRASE,
    ContentHash,
    SeriesBindingRef,
    VariantRef,
)
from tests.fsops_env import INSTANCE_ID
from tests.tombstone_env import HASH, PLACE, SERIES, SLUG, TITLE, VARIANT, make_tomb_env

pytestmark = pytest.mark.req("EF-BIB-054", "ENF-SEC-005")

slugs = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=4, max_size=40)


def _probes(word: str) -> list[bytes]:
    """What a dictionary attacker computes for one candidate word, in every usual encoding."""
    raw = word.encode()
    digests = [
        hashlib.sha256(raw).digest(),
        hashlib.sha256(INSTANCE_ID.encode() + raw).digest(),
        hmac.new(INSTANCE_ID.encode(), raw, hashlib.sha256).digest(),
    ]
    out = [raw]
    for digest in digests:
        out += [digest, digest.hex().encode(), base64.urlsafe_b64encode(digest).rstrip(b"=")]
    return out


def _everything_on_disk(env_tmp: Path, database: Path) -> bytes:
    """The database file, every journal and every sentinel, as an attacker copying them sees."""
    blobs = [database.read_bytes()]
    blobs += [path.read_bytes() for path in env_tmp.rglob("tombstones")]
    blobs += [path.read_bytes() for path in env_tmp.rglob(".shelfhold-mount-id")]
    with closing(sqlite3.connect(database)) as db:
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            for row in db.execute(f'SELECT * FROM "{name}"'):  # noqa: S608
                blobs.append(repr(row).encode())
    return b"\n".join(blobs)


@settings(max_examples=12)
@given(words=st.lists(slugs, min_size=1, max_size=5, unique=True), hex_hash=st.binary(32, 32))
def test_tombstone_values_encrypted_not_dictionary_reversible(
    words: list[str], hex_hash: bytes
) -> None:
    """Table, journal, a dictionary of title slugs and the known instance_id: no match, and no
    `content_hash` in clear."""
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        env = make_tomb_env(tmp)
        service = env.service()
        service.replay(env.roots)
        content = "sha256:" + hex_hash.hex()
        for word in words:
            hashes = (ContentHash("content-hash@1", content),)
            service.record(VariantRef("suwayomi", "77", f"/chapter/{word}", hashes), env.roots)
            service.record(SeriesBindingRef("suwayomi", "77", f"/manga/{word}"), env.roots)
        everything = _everything_on_disk(tmp, tmp / "shelfhold.db")
        for word in words:
            for probe in _probes(word) + _probes(f"/manga/{word}"):
                assert probe not in everything
        assert hex_hash.hex().encode() not in everything
        assert hex_hash not in everything
        # The same values sealed twice give unrelated ciphertexts: no confirmation by equality.
        cts = [row["ct"] for row in env.table_rows()]
        assert len(set(cts)) == len(cts)
        # A key derived from the public instance_id does not open anything.
        guess = hkdf_sha256(INSTANCE_ID.encode(), info=b"shelfhold/tombstone/v1")
        row = env.table_rows()[0]
        with pytest.raises(DecryptionError):
            open_values(guess, row["nonce"], row["ct"], scope=row["scope"], root="db", seq=1)


_MUTATIONS = st.one_of(
    st.just(("copy_to_other_root", 0)),
    st.tuples(st.just("flip_ct"), st.integers(0, 10_000)),
    st.tuples(st.just("flip_nonce"), st.integers(0, 23)),
    st.just(("change_seq", 0)),
    st.just(("change_scope", 0)),
)


def _flip(text: str, index: int) -> str:
    raw = bytearray(base64.urlsafe_b64decode(text))
    raw[index % len(raw)] ^= 0x01
    return base64.urlsafe_b64encode(bytes(raw)).decode()


@settings(max_examples=15)
@given(mutation=_MUTATIONS)
def test_tombstone_line_bound_to_root_and_scope(mutation: tuple[str, int]) -> None:
    """A line copied to another root, or changed by one bit, does not open: it stays inert."""
    kind, where = mutation
    with tempfile.TemporaryDirectory() as raw:
        env = make_tomb_env(Path(raw))
        service = env.service()
        service.replay(env.roots)
        service.record(SERIES, [env.library])
        line = json.loads(env.journal(env.library).splitlines()[1])
        with env.engine.begin() as connection:  # only the journal line may block from now on
            connection.execute(sa.delete(coherence.content_tombstone))
        target = env.library
        if kind == "copy_to_other_root":
            env.journal_path(env.library).unlink()
            target = env.books
        elif kind == "flip_ct":
            line["ct"] = _flip(line["ct"], where)
        elif kind == "flip_nonce":
            line["nonce"] = _flip(line["nonce"], where)
        elif kind == "change_seq":
            line["seq"] += 1
        else:
            line["scope"] = "work"
        tampered = json.dumps(line, separators=(",", ":")).encode() + b"\n"
        if kind != "copy_to_other_root":
            env.journal_path(env.library).unlink()
        header = b'{"kind":"shelfhold.tombstones","schema_version":"2.0"}\n'
        env.fs.fsops.append_tombstone_line(target.path, tampered, header=header)

        report = env.service().replay(env.roots)

        assert report.verified == 0
        assert [(i.root_id, i.reason) for i in report.inert] == [
            (target.root_id, "authentication_failed")
        ]
        fresh = env.services[-1]
        assert not fresh.check(SERIES, root_id=target.root_id).blocked
        assert env.events("tombstone.unverified")[-1]["reason"] == "authentication_failed"


def test_tombstone_and_audit_hold_no_title(tmp_path: Path) -> None:
    """I36: table, journal, sentinel, events and audit hold neither the title nor a raw value."""
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    for values in (VARIANT, PLACE, SERIES):
        service.record(values, [env.library, env.unmounted(env.books)])
    service.replay(env.roots)  # flushes the pending line of `books`
    env.store.write(Keyring.generate())  # the keyring is lost
    lost = env.service()
    lost.replay(env.roots)
    lost.accept_lost_keyring(env.roots, confirmation=LOST_KEYRING_PHRASE, actor_id=None)
    everything = _everything_on_disk(tmp_path, tmp_path / "shelfhold.db")
    for secret in (TITLE, SLUG, HASH, HASH.removeprefix("sha256:"), "/chapter/", "/manga/"):
        assert secret.encode() not in everything, secret
    assert env.journal(env.books).count(b"\n") == 4  # header and three lines, flushed
    with env.engine.connect() as connection:
        audit = connection.execute(sa.select(identity.audit)).mappings().all()
        events = connection.execute(sa.select(platform.event.c.payload)).scalars().all()
    assert [row["action"] for row in audit] == ["tombstone.accept_lost_keyring"]
    assert all(TITLE not in str(row) and SLUG not in str(row) for row in audit)
    assert all(SLUG not in json.dumps(payload) for payload in events)
