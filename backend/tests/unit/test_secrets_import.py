"""ENF-SEC-005 (M1b L06): what `secrets import` accepts from an export file.

The merge never overwrites a key and a forged file imports nothing. Everything here is the pure
half (the merge, the reading of the files); `tests/integration/test_secrets_import.py` plays the
real command over it.
"""
# ruff: noqa: S105 (synthetic passphrases)

import base64
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pyrage
import pytest

from shelfhold.infrastructure.secrets.envelope import KEY_BYTES
from shelfhold.infrastructure.secrets.errors import ExportError, KeyConflictError
from shelfhold.infrastructure.secrets.export import (
    MAX_EXPORT_BYTES,
    armor,
    decrypt_export,
    encrypt_export,
    read_export_file,
    read_passphrase_file,
)
from shelfhold.infrastructure.secrets.keyring import Key, Keyring

pytestmark = pytest.mark.req("ENF-SEC-005")

PASSPHRASE = "synthetic passphrase for the import tests"
NOW = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


def keyring_with_history() -> Keyring:
    return Keyring.generate().rotated().rotation_completed()


def entry(kid: str, material: bytes, state: str = "active") -> dict[str, object]:
    return {
        "kid": kid,
        "alg": "xchacha20poly1305",
        "state": state,
        "key": base64.b64encode(material).decode(),
        "created_at": NOW.isoformat(),
        "retired_at": None if state == "active" else NOW.isoformat(),
    }


def forged(document: object) -> str:
    """An armored age file around any JSON value, as an attacker who knows the passphrase would
    make it (the file is authentic as age, its content is not a keyring)."""
    raw = json.dumps(document).encode() if not isinstance(document, bytes) else document
    return armor(pyrage.passphrase.encrypt(raw, PASSPHRASE))


def test_merge_adds_unknown_kids_as_retired_and_keeps_the_active_key() -> None:
    local = Keyring.generate()
    incoming = keyring_with_history()
    merged = local.merge(incoming, now=NOW)
    assert merged.keyring.active == local.active
    assert set(merged.added) == {key.kid for key in incoming.keys}
    for key in incoming.keys:
        found = merged.keyring.get(key.kid)
        assert found.material == key.material
        assert found.state == "retired" and found.retired_at is not None
    assert [k.state for k in merged.keyring.keys].count("active") == 1


def test_merge_is_idempotent_and_changes_nothing_the_second_time() -> None:
    local, incoming = Keyring.generate(), keyring_with_history()
    once = local.merge(incoming, now=NOW)
    again = once.keyring.merge(incoming, now=NOW)
    assert again.added == () and again.keyring.keys == once.keyring.keys


def test_merge_with_an_already_known_key_keeps_the_local_state() -> None:
    incoming = keyring_with_history()
    retired = next(k for k in incoming.keys if k.state == "retired")
    local = Keyring(keys=(incoming.active, retired))
    merged = local.merge(incoming, now=NOW)
    assert merged.added == () and merged.keyring.keys == local.keys


def test_merge_refuses_a_kid_with_other_material_and_names_only_the_kid() -> None:
    incoming = Keyring.generate()
    impostor = Key(kid=incoming.active.kid, material=os.urandom(KEY_BYTES))
    with pytest.raises(KeyConflictError) as refused:
        Keyring(keys=(impostor,)).merge(incoming, now=NOW)
    assert incoming.active.kid in str(refused.value)
    for material in (incoming.active.material, impostor.material):
        assert base64.b64encode(material).decode() not in str(refused.value)
        assert material.hex() not in str(refused.value)


def test_merge_never_adopts_the_rotation_marker_or_a_second_active_key() -> None:
    incoming = Keyring.generate().rotated()  # a rotation in progress on the other instance
    merged = Keyring.generate().merge(incoming, now=NOW)
    assert merged.keyring.rotation is None
    assert [k.state for k in merged.keyring.keys].count("active") == 1


def test_export_roundtrip_through_the_reader() -> None:
    keyring = keyring_with_history()
    text = encrypt_export(keyring, PASSPHRASE)
    assert decrypt_export(text, PASSPHRASE).keys == keyring.keys


GOOD = encrypt_export(keyring_with_history(), PASSPHRASE)
DUPLICATE_KID = [
    entry("k-aaaaaaaaaaaaaaaa", b"\x01" * KEY_BYTES),
    entry("k-aaaaaaaaaaaaaaaa", b"\x02" * KEY_BYTES, "retired"),
]


def _document(keys: list[dict[str, object]]) -> dict[str, object]:
    return {"format": 1, "keys": keys, "rotation": None, "rotated_at": None, "exported_at": None}


FORGERIES = {
    "truncated armor": GOOD[: len(GOOD) // 2],
    "truncated binary": armor(pyrage.passphrase.encrypt(b"x" * 200, PASSPHRASE)[:-7]),
    "wrong begin line": GOOD.replace("BEGIN AGE ENCRYPTED FILE", "BEGIN PGP MESSAGE", 1),
    "wrong end line": GOOD.replace("END AGE ENCRYPTED FILE", "END PGP MESSAGE", 1),
    "not base64": GOOD.replace("\n", "\n!!", 2),
    "empty": "",
    "another passphrase": encrypt_export(Keyring.generate(), "not the passphrase at all"),
    "duplicate kid": forged(_document(DUPLICATE_KID)),  # two active ones as well
    "duplicate kid, one retired": forged(
        _document(
            [
                entry("k-aaaaaaaaaaaaaaaa", b"\x01" * KEY_BYTES, "retired"),
                entry("k-aaaaaaaaaaaaaaaa", b"\x01" * KEY_BYTES, "retired"),
                entry("k-bbbbbbbbbbbbbbbb", b"\x03" * KEY_BYTES),
            ]
        )
    ),
    "key of the wrong size": forged(_document([entry("k-aaaaaaaaaaaaaaaa", b"\x01" * 16)])),
    "key too long": forged(_document([entry("k-aaaaaaaaaaaaaaaa", b"\x01" * 64)])),
    "no active key": forged(
        _document([entry("k-aaaaaaaaaaaaaaaa", b"\x01" * KEY_BYTES, "retired")])
    ),
    "no key": forged(_document([])),
    "unknown format": forged(
        {**_document([entry("k-aaaaaaaaaaaaaaaa", b"\x01" * KEY_BYTES)]), "format": 2}
    ),
    "kid with a line break": forged(_document([entry("k-aa\nbb", b"\x01" * KEY_BYTES)])),
    "kid too long": forged(_document([entry("k-" + "a" * 500, b"\x01" * KEY_BYTES)])),
    "kid that is not text": forged(_document([{**entry("k-x", b"\x01" * KEY_BYTES), "kid": 7}])),
    "unknown algorithm": forged(
        _document([{**entry("k-aaaaaaaaaaaaaaaa", b"\x01" * KEY_BYTES), "alg": "rot13"}])
    ),
    "naive date": forged(
        _document(
            [
                {
                    **entry("k-aaaaaaaaaaaaaaaa", b"\x01" * KEY_BYTES),
                    "created_at": "2026-01-01T00:00:00",
                }
            ]
        )
    ),
    "not a document": forged([1, 2, 3]),
    "not json": forged(b"\xff\xfe{"),
    "json nested too deep": forged(b"[" * 200_000),
    "too many keys": forged(
        _document(
            [
                entry(f"k-{n:016x}", bytes([n % 250 + 1]) * KEY_BYTES, "retired")
                for n in range(1, 1500)
            ]
            + [entry("k-ffffffffffffffff", b"\x09" * KEY_BYTES)]
        )
    ),
}


@pytest.mark.parametrize("label", sorted(FORGERIES))
def test_a_forged_export_is_refused_and_says_nothing_of_its_content(label: str) -> None:
    with pytest.raises(ExportError) as refused:
        decrypt_export(FORGERIES[label], PASSPHRASE)
    text = str(refused.value)
    assert len(text) < 200
    assert PASSPHRASE not in text
    assert "\\x01" not in text and "AQEB" not in text  # no key material echoed


def _with_log_n(log_n: str) -> str:
    binary = pyrage.passphrase.encrypt(b"x" * 50, PASSPHRASE)
    head, rest = binary.split(b"\n---", 1)
    lines = head.split(b"\n")
    parts = lines[1].split(b" ")
    parts[3] = log_n.encode()
    lines[1] = b" ".join(parts)
    return armor(b"\n".join(lines) + b"\n---" + rest)


@pytest.mark.parametrize("log_n", ["21", "23", "63", "255", "99999", "0", "-1", "x"])
def test_a_forged_scrypt_cost_is_refused_before_any_derivation(
    log_n: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def derivation(*_a: object, **_k: object) -> bytes:
        raise AssertionError("the library was called")

    monkeypatch.setattr(pyrage.passphrase, "decrypt", derivation)
    with pytest.raises(ExportError, match="wrong passphrase or damaged file"):
        decrypt_export(_with_log_n(log_n), PASSPHRASE)


def test_the_cost_age_writes_by_default_is_accepted() -> None:
    assert decrypt_export(GOOD, PASSPHRASE).keys


def test_a_giant_export_file_is_refused_before_it_is_decrypted(tmp_path: Path) -> None:
    path = tmp_path / "giant.age"
    path.write_bytes(b"A" * (MAX_EXPORT_BYTES + 1))
    with pytest.raises(ExportError, match="too large"):
        read_export_file(path)


def test_an_export_file_must_be_a_regular_ascii_file(tmp_path: Path) -> None:
    binary = tmp_path / "binary.age"
    binary.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ExportError):
        read_export_file(binary)
    with pytest.raises(ExportError):
        read_export_file(tmp_path)  # a directory
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ExportError):
        read_export_file(fifo)  # never blocks on a pipe
    with pytest.raises(ExportError):
        read_export_file(tmp_path / "absent.age")


def passphrase_file(tmp_path: Path, mode: int) -> Path:
    path = tmp_path / f"pass-{mode:o}"
    path.write_text(PASSPHRASE + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666, 0o660])
def test_a_passphrase_file_open_to_other_users_is_refused(tmp_path: Path, mode: int) -> None:
    with pytest.raises(ExportError) as refused:
        read_passphrase_file(passphrase_file(tmp_path, mode))
    assert PASSPHRASE not in str(refused.value)
    assert "chmod 600" in str(refused.value)


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_a_passphrase_file_for_the_owner_alone_is_read(tmp_path: Path, mode: int) -> None:
    path = passphrase_file(tmp_path, mode)
    assert stat.S_IMODE(path.stat().st_mode) == mode
    assert read_passphrase_file(path) == PASSPHRASE


def test_a_passphrase_file_that_is_not_a_regular_file_is_refused(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ExportError):
        read_passphrase_file(fifo)  # never blocks on a pipe
    with pytest.raises(ExportError):
        read_passphrase_file(tmp_path)
