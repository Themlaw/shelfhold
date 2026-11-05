"""Journal 2.0 on disk, the per-kid key derivation and the sentinel's kid registry (L33)."""

import json
import os
import stat
from pathlib import Path

import pytest

from shelfhold.infrastructure.fsops.errors import ReservedPathError
from shelfhold.infrastructure.secrets.tombstone_aead import (
    build_aad,
    hkdf_sha256,
    open_values,
    seal_values,
)
from shelfhold.library.tombstones.journal import (
    MAX_JOURNAL_BYTES,
    MAX_LINE_BYTES,
    JournalFormatError,
    JournalLine,
    header_bytes,
    parse_journal,
)
from shelfhold.library.tombstones.values import (
    PAD_TO,
    TombstoneValueError,
    decode_plaintext,
    encode_plaintext,
)
from tests.fsops_env import make_env
from tests.tombstone_env import PLACE, SERIES, VARIANT

pytestmark = pytest.mark.req("EF-BIB-054", "EF-BIB-053")


@pytest.mark.req("ENF-SEC-005")
def test_hkdf_sha256_matches_rfc5869_case_1() -> None:
    ikm = bytes.fromhex("0b" * 22)
    salt = bytes.fromhex("000102030405060708090a0b0c")
    info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
    okm = hkdf_sha256(ikm, salt=salt, info=info, length=42)
    assert okm.hex() == (
        "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"
    )


def test_aad_binds_format_scope_root_and_seq() -> None:
    key = os.urandom(32)
    nonce, ct = seal_values(key, b"x", scope="place", root="R1", seq=3)
    assert open_values(key, nonce, ct, scope="place", root="R1", seq=3) == b"x"
    for scope, root, seq in (("variant", "R1", 3), ("place", "R2", 3), ("place", "R1", 4)):
        with pytest.raises(Exception, match="authenticate"):
            open_values(key, nonce, ct, scope=scope, root=root, seq=seq)
    assert build_aad("2.0", "a", "bc", 1) != build_aad("2.0", "ab", "c", 1)


def test_plaintext_is_padded_strict_and_round_trips() -> None:
    for values in (VARIANT, PLACE, SERIES):
        raw = encode_plaintext(values, {"number": "number@1"})
        assert len(raw) % PAD_TO == 0
        assert decode_plaintext(raw, values.scope) == (values, {"number": "number@1"})
    raw = encode_plaintext(SERIES, {})
    with pytest.raises(TombstoneValueError):
        decode_plaintext(raw, "variant")  # authenticated scope and clear text must agree
    with pytest.raises(TombstoneValueError):
        decode_plaintext(json.dumps({"f": 1}).encode(), "series_binding")


def test_journal_parse_is_bounded_and_strict() -> None:
    good = JournalLine(1, "place", "k-1", b"\0" * 24, b"\1" * 32).dumps()
    extra = json.loads(good)
    extra["title"] = "x"
    rows = [
        header_bytes(),
        good,
        json.dumps(extra).encode() + b"\n",
        b"not json\n",
        b"{" + b" " * MAX_LINE_BYTES + b"}\n",
        good.replace(b'"seq":1', b'"seq":true'),
    ]
    journal = parse_journal(b"".join(rows))
    assert [line.seq for line in journal.lines] == [1]
    assert len(journal.malformed) == 4
    with pytest.raises(JournalFormatError):
        parse_journal(good)  # no header
    with pytest.raises(JournalFormatError):
        parse_journal(b'{"kind":"shelfhold.tombstones","schema_version":"3.0"}\n' + good)
    with pytest.raises(JournalFormatError):
        parse_journal(b"x" * (MAX_JOURNAL_BYTES + 1))
    newer_minor = b'{"kind":"shelfhold.tombstones","schema_version":"2.7","later":1}\n'
    assert len(parse_journal(newer_minor + good).lines) == 1


def test_journal_is_appended_owner_only_and_never_reachable_by_a_plan(tmp_path: Path) -> None:
    env = make_env(tmp_path, umask=0o022)
    first = JournalLine(1, "place", "k-1", b"\0" * 24, b"\1" * 32).dumps()
    second = JournalLine(2, "place", "k-1", b"\2" * 24, b"\3" * 32).dumps()
    env.fsops.append_tombstone_line(str(env.library), first, header=header_bytes())
    env.fsops.append_tombstone_line(str(env.library), second, header=header_bytes())
    path = env.library / ".shelfhold" / "tombstones"
    assert path.read_bytes() == header_bytes() + first + second
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(ValueError, match="one line"):
        env.fsops.append_tombstone_line(str(env.library), b"a\nb\n", header=header_bytes())
    with pytest.raises(ReservedPathError):
        env.fsops.write_atomic(str(path), b"rewritten")
    with pytest.raises(ReservedPathError):
        env.fsops.unlink(str(path))


def test_sentinel_kid_registry_only_grows_and_keeps_unknown_fields(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    sentinel = env.library / ".shelfhold-mount-id"
    doc = json.loads(sentinel.read_text())
    doc["written_by_m1b"] = {"tombstone_count": 4}
    sentinel.write_text(json.dumps(doc))
    for kid in ("k-a", "k-b", "k-a"):
        env.fsops.record_tombstone_kid(str(env.library), kid)
    after = json.loads(sentinel.read_text())
    assert after["tombstone_kids"] == ["k-a", "k-b"]
    assert after["written_by_m1b"] == {"tombstone_count": 4}
    assert after["schema_version"] == "1.1"
    assert after["generation"] == doc["generation"]
    assert env.fsops.tombstone_kids(str(env.library)) == {"k-a", "k-b"}
    assert env.fsops.tombstone_kids(str(env.books)) == frozenset()
