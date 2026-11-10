"""EF-INT-015, ENF-SEC-005 (M1b L23): the archive format and the forged archive.

Nothing here touches a file system but a temporary folder: the point is that no entry of an
archive can write outside the destination, and that a truncated or altered archive is refused
with one sentence that says nothing of its content.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shelfhold.infrastructure.secrets.export import encrypt_export
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.ops.backup import BackupArchiveError, DestinationError, restore_archive
from shelfhold.ops.backup import archive as arc
from shelfhold.ops.backup.verify import check_file
from tests.backup_env import PASSPHRASE, Collect, write_passphrase

pytestmark = pytest.mark.req("EF-INT-015", "ENF-SEC-005")

NOW = datetime(2026, 11, 8, 1, 40, tzinfo=UTC)
OTHER = "another synthetic passphrase 6789"


def tar_bytes(members: dict[str, bytes], revision: str = "0099") -> bytes:
    return b"".join(arc.iter_tar(members, created_at=NOW, schema_revision=revision))


def unpack(tar: bytes, step: int = 1000) -> tuple[arc.Parsed, dict[str, bytes]]:
    """The tar through the push parser, in awkward chunks (a header can straddle two)."""
    handler = Collect()
    parser = arc.TarParser(handler)
    for start in range(0, len(tar), step):
        parser.write(tar[start : start + step])
    return parser.finish(), handler.members


def seal(tar: bytes, passphrase: str = PASSPHRASE) -> bytes:
    out = io.BytesIO()
    arc.write_archive(out, iter([tar[i : i + 4096] for i in range(0, len(tar), 4096)]), passphrase)
    return out.getvalue()


def check_blob(blob: bytes, passphrase: str, folder: Path):  # type: ignore[no-untyped-def]
    path = folder / "candidate.tar.age"
    path.write_bytes(blob)
    return check_file(path, passphrase, folder)


def sqlite_bytes() -> bytes:
    import sqlite3

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
    connection.execute("INSERT INTO alembic_version VALUES ('0099')")
    connection.commit()
    try:
        return connection.serialize()
    finally:
        connection.close()


def good_members() -> dict[str, bytes]:
    return {
        arc.DATABASE_MEMBER: sqlite_bytes(),
        arc.KEYRING_MEMBER: encrypt_export(Keyring.generate(NOW), PASSPHRASE).encode("ascii"),
        "config.toml": b'schema_version = "1.0"\n',
        "meta/units/ab/X.json": b"{}",
        "docs/schema.sql": b"-- schema\n",
    }


def tar_of(entries: list[tuple[str, bytes, bytes]]) -> bytes:
    """A hand-made ustar tar: (name, data, typeflag). For names `pack` would refuse."""
    out = bytearray()
    for name, data, flag in entries:
        head = bytearray(512)
        head[0 : len(name.encode())] = name.encode()
        head[100:108] = b"0000600\x00"
        head[124:136] = f"{len(data):011o}".encode() + b"\x00"
        head[148:156] = b" " * 8
        head[156:157] = flag
        head[257:263] = b"ustar\x00"
        head[263:265] = b"00"
        head[148:156] = f"{sum(head):06o}".encode() + b"\x00 "
        out += head + data + bytes(-len(data) % 512)
    return bytes(out + bytes(1024))


def manifest_for(members: dict[str, bytes]) -> bytes:
    return json.dumps(
        {
            "format": arc.FORMAT,
            "created_at": "2026-11-08T01:40:00Z",
            "schema_revision": "0099",
            "members": {
                n: {"sha256": hashlib.sha256(d).hexdigest(), "size": len(d)}
                for n, d in members.items()
            },
        }
    ).encode()


def forged_tar(extra_name: str, flag: bytes = b"0", data: bytes = b"x") -> bytes:
    """A tar whose manifest lists the extra entry too: only its name (or type) is wrong."""
    members = {**good_members(), extra_name: data}
    entries = [(arc.MANIFEST_MEMBER, manifest_for(members), b"0")]
    entries += [(n, d, flag if n == extra_name else b"0") for n, d in sorted(members.items())]
    return tar_of(entries)


def forged(extra_name: str, flag: bytes = b"0", data: bytes = b"x") -> bytes:
    return seal(forged_tar(extra_name, flag, data))


def write_archive(tmp_path: Path, blob: bytes) -> Path:
    path = tmp_path / "forged.tar.age"
    path.write_bytes(blob)
    return path


def snapshot(folder: Path) -> list[str]:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))


def test_a_packed_archive_round_trips_and_is_a_plain_ustar_tar(tmp_path: Path) -> None:
    members = good_members()
    packed = tar_bytes(members)
    parsed, found = unpack(packed)
    assert found == members and parsed.schema_revision == "0099"
    assert dict(parsed.members) == {n: len(d) for n, d in members.items()}
    # readable by any tar: the format is not ours alone
    path = tmp_path / "set.tar"
    path.write_bytes(packed)
    listing = subprocess.run(  # noqa: S603
        ["tar", "tf", str(path)],  # noqa: S607
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert set(listing.stdout.split()) == {arc.MANIFEST_MEMBER, *members}


@pytest.mark.parametrize(
    "name",
    [
        "../escape.txt",
        "meta/../../escape.txt",
        "/etc/escape.txt",
        "meta//x",
        "./config2.toml",
        "meta\\x",
        "unknown/x",
        "meta",
        "meta/",
        "docs/" + "d/" * 200 + "x",
    ],
)
def test_forged_member_names_are_refused(name: str) -> None:
    with pytest.raises(BackupArchiveError):
        unpack(forged_tar(name))


@pytest.mark.parametrize("name", ["../escape.txt", "/etc/escape.txt", "meta/../../escape.txt"])
def test_a_forged_archive_restores_nothing_and_writes_nowhere(tmp_path: Path, name: str) -> None:
    """End to end, through the decryption: the restore of an archive with an escaping name stops
    before the first file, and nothing exists outside the destination (which is not created)."""
    folder = tmp_path / "work"
    folder.mkdir()
    archive = write_archive(folder, forged(name))
    destination = folder / "dest"
    with pytest.raises(BackupArchiveError):
        restore_archive(archive, destination, write_passphrase(tmp_path / "pass"))
    assert not destination.exists() and snapshot(folder) == ["forged.tar.age"]
    assert not (tmp_path / "escape.txt").exists() and not Path("/etc/escape.txt").exists()


@pytest.mark.parametrize("flag", [b"1", b"2", b"3", b"4", b"5", b"6", b"x", b"g", b"L", b"K"])
def test_a_link_a_device_a_folder_or_an_extension_header_is_refused(flag: bytes) -> None:
    """A symbolic link (flag 2) pointing out of the destination is the classic; every non-regular
    type is refused alike, before anything is created."""
    with pytest.raises(BackupArchiveError):
        unpack(forged_tar("meta/units/link", flag=flag, data=b""))


def test_a_forged_symbolic_link_archive_restores_nothing(tmp_path: Path) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    archive = write_archive(folder, forged("meta/units/link", flag=b"2", data=b""))
    with pytest.raises(BackupArchiveError):
        restore_archive(archive, folder / "dest", write_passphrase(tmp_path / "pass"))
    assert snapshot(folder) == ["forged.tar.age"]


def test_a_duplicate_member_and_a_member_the_manifest_does_not_list_are_refused() -> None:
    members = good_members()
    manifest = manifest_for(members)
    twice = [(arc.MANIFEST_MEMBER, manifest, b"0"), *[(n, d, b"0") for n, d in members.items()]]
    twice.append(("config.toml", b"evil", b"0"))
    with pytest.raises(BackupArchiveError):
        unpack(tar_of(twice))
    unlisted = [(arc.MANIFEST_MEMBER, manifest, b"0"), *[(n, d, b"0") for n, d in members.items()]]
    unlisted.append(("meta/extra.json", b"{}", b"0"))
    with pytest.raises(BackupArchiveError):
        unpack(tar_of(unlisted))
    swapped = [
        (arc.MANIFEST_MEMBER, manifest, b"0"),
        *[(n, d + b"!", b"0") for n, d in members.items()],
    ]
    with pytest.raises(BackupArchiveError):  # a member whose bytes are not the manifest's
        unpack(tar_of(swapped))
    manifest_last = [(n, d, b"0") for n, d in members.items()] + [
        (arc.MANIFEST_MEMBER, manifest, b"0")
    ]
    with pytest.raises(BackupArchiveError):
        unpack(tar_of(manifest_last))


def test_truncated_altered_and_wrongly_keyed_archives_give_one_message(tmp_path: Path) -> None:
    good = seal(tar_bytes(good_members()))
    assert check_blob(good, PASSPHRASE, tmp_path).member_count == 5
    flipped = bytearray(good)
    flipped[len(good) // 2] ^= 0x01
    header_flipped = bytearray(good)
    header_flipped[len(arc.ARCHIVE_MAGIC) + 6] ^= 0x01  # inside the sealed key
    bad = {
        "truncated": good[: len(good) // 2],
        "cut by one byte": good[:-1],
        "altered": bytes(flipped),
        "key altered": bytes(header_flipped),
        "empty": b"",
        "not age": b"hello" * 100,
        "no header": good[len(arc.ARCHIVE_MAGIC) :],
        "wrong passphrase": good,
    }
    messages = set()
    for label, blob in bad.items():
        passphrase = OTHER if label == "wrong passphrase" else PASSPHRASE
        with pytest.raises(BackupArchiveError) as refused:
            check_blob(blob, passphrase, tmp_path)
        messages.add(str(refused.value))
    assert messages == {arc.GENERIC}  # the same sentence, whatever the damage
    # a tar cut before its end (a cipher text that decrypts to one) is refused too
    tar = tar_bytes(good_members())
    for cut in (len(tar) - 1024, len(tar) - 1, 700, 512):
        with pytest.raises(BackupArchiveError):
            unpack(tar[:cut])  # the end of the stream is where the cut is noticed


def test_a_database_that_fails_its_integrity_check_is_refused(tmp_path: Path) -> None:
    members = {**good_members(), arc.DATABASE_MEMBER: b"SQLite format 3\x00" + os.urandom(4096)}
    with pytest.raises(BackupArchiveError):
        check_blob(seal(tar_bytes(members)), PASSPHRASE, tmp_path)


def test_a_planted_link_in_the_destination_is_not_followed(tmp_path: Path) -> None:
    """The destination is empty or new; one made by someone else with a link inside (a race) is
    refused as not empty, and a link planted in a folder the restore made is never followed."""
    archive = write_archive(tmp_path, seal(tar_bytes(good_members())))
    passphrase = write_passphrase(tmp_path / "pass")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(outside)
    with pytest.raises(DestinationError):
        restore_archive(archive, link, passphrase)  # a link is not a destination
    assert list(outside.iterdir()) == []
    taken = tmp_path / "taken"
    taken.mkdir()
    (taken / "x").write_text("x")
    with pytest.raises(DestinationError):
        restore_archive(archive, taken, passphrase)


def test_the_documented_two_step_opens_the_archive_with_plain_age_and_tar(tmp_path: Path) -> None:
    """`docs/admin/sauvegarde.md`: the offsets the shell recipe computes (`head -n 1`, `wc -c`)
    cut the file into an age file sealed by the passphrase and an age file for its key; the
    second opens the tar. Only pyrage (the age implementation) and `tar` are used here."""
    import pyrage

    blob = seal(tar_bytes(good_members()))
    first = blob.split(b"\n", 1)[0]
    size, line = int(first.split(b" ")[1]), len(first) + 1
    sealed, body = blob[line : line + size], blob[line + size :]
    secret = pyrage.passphrase.decrypt(sealed, PASSPHRASE).decode("ascii")
    assert secret.startswith("AGE-SECRET-KEY-1")  # what `age -d -i` reads from a file
    clear = pyrage.decrypt(body, [pyrage.x25519.Identity.from_str(secret)])
    path = tmp_path / "set.tar"
    path.write_bytes(clear)
    listing = subprocess.run(  # noqa: S603
        ["tar", "tf", str(path)],  # noqa: S607
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert set(listing.stdout.split()) == {arc.MANIFEST_MEMBER, *good_members()}


def test_the_length_in_the_header_is_ascii_digits_and_nothing_else(tmp_path: Path) -> None:
    good = seal(tar_bytes(good_members()))
    first, rest = good.split(b"\n", 1)
    size = first[len(arc.ARCHIVE_MAGIC) :]
    assert check_blob(good, PASSPHRASE, tmp_path).member_count == 5
    arabic = "".join(chr(0x660 + int(d)) for d in size.decode()).encode()
    variants = {
        "a sign": b"+" + size,
        "a space before": b" " + size,
        "a space after": size + b" ",
        "a leading zero": b"0" + size,
        "an underscore": size[:1] + b"_" + size[1:],
        "other digits": arabic,
        "a hex number": b"0x" + size,
        "no number": b"",
        "too many digits": b"1" * 9,
    }
    for label, variant in variants.items():
        forged = arc.ARCHIVE_MAGIC + variant + b"\n" + rest
        with pytest.raises(BackupArchiveError):
            check_blob(forged, PASSPHRASE, tmp_path)
        assert label


def test_the_ephemeral_key_is_masked_in_logs_while_it_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(arc, "register_secret_value", seen.append)
    blob = seal(tar_bytes(good_members()))
    check_blob(blob, PASSPHRASE, tmp_path)
    assert len(seen) == 2 and seen[0] == seen[1]  # the same key, on the way in and on the way out
    assert seen[0].startswith("AGE-SECRET-KEY-1") and seen[0].encode() not in blob
