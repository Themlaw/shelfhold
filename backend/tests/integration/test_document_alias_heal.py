"""M4 L14a, carried over from L13 (ENF-QUAL-005): the hashes of `document_alias` are filled by a
hook after the commit, so a crash between the two leaves them empty. A sweep closes the gap: it
fills the alias of a file it finds, after checking that the file is the one the alias names."""

import hashlib
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.export.application.render_hooks import heal_alias_hashes
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.reading.domain.document_hash import kavita_hash, koreader_partial_md5
from tests.reading_env import reading_env

pytestmark = pytest.mark.req("ENF-QUAL-005")


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def alias_row(env, item: str, data: bytes) -> str:  # type: ignore[no-untyped-def]
    with env.engine.begin() as connection:
        connection.execute(
            sa.insert(export.document_alias).values(
                export_item_id=item, file_hash=sha(data), content_hash=sha(data + b"c")
            )
        )
    return sha(data)


def read(env, item: str, file_hash: str):  # type: ignore[no-untyped-def]
    with env.engine.connect() as connection:
        return connection.execute(
            sa.select(export.document_alias).where(
                export.document_alias.c.export_item_id == item,
                export.document_alias.c.file_hash == file_hash,
            )
        ).one()


def test_the_sweep_fills_the_hashes_a_crash_left_empty(tmp_path: Path) -> None:
    data = bytes(range(256)) * 20
    file = tmp_path / "volume.cbz"
    file.write_bytes(data)
    with reading_env(tmp_path) as env:
        item = env.file("ok", env.units("ok", 1))
        file_hash = alias_row(env, item, data)
        done = heal_alias_hashes(env.engine, locate=lambda _item, _hash: file)
        row = read(env, item, file_hash)
        assert done == 1
        assert row.koreader_partial_md5 == "md5:" + koreader_partial_md5(file)
        assert row.kavita_md5 == "md5:" + kavita_hash(file).lower()
        assert heal_alias_hashes(env.engine, locate=lambda *_: file) == 0  # nothing left to do


def test_the_sweep_never_hashes_a_file_that_is_not_the_one_named(tmp_path: Path) -> None:
    """The file at the path changed since: its hashes must not land on the alias of another."""
    file = tmp_path / "volume.cbz"
    file.write_bytes(b"something else entirely")
    with reading_env(tmp_path) as env:
        item = env.file("ok", env.units("ok", 1))
        file_hash = alias_row(env, item, b"what the alias names")
        assert heal_alias_hashes(env.engine, locate=lambda *_: file) == 0
        row = read(env, item, file_hash)
        assert row.koreader_partial_md5 is None and row.kavita_md5 is None


def test_the_sweep_skips_a_file_that_is_gone_and_goes_on(tmp_path: Path) -> None:
    good = tmp_path / "good.cbz"
    good.write_bytes(b"good file bytes")
    with reading_env(tmp_path) as env:
        item = env.file("ok", env.units("ok", 1))
        gone = alias_row(env, item, b"replaced and gone")
        kept = alias_row(env, item, b"good file bytes")

        def locate(_item: str, file_hash: str) -> Path | None:
            return good if file_hash == kept else None

        assert heal_alias_hashes(env.engine, locate=locate) == 1
        assert read(env, item, gone).koreader_partial_md5 is None  # honest: not invented
        assert read(env, item, kept).koreader_partial_md5 is not None


def test_a_locator_that_fails_does_not_stop_the_sweep(tmp_path: Path) -> None:
    good = tmp_path / "good.cbz"
    good.write_bytes(b"good file bytes")
    with reading_env(tmp_path) as env:
        item = env.file("ok", env.units("ok", 1))
        broken = alias_row(env, item, b"broken")
        kept = alias_row(env, item, b"good file bytes")

        def locate(_item: str, file_hash: str) -> Path | None:
            if file_hash == broken:
                raise OSError("a share is down")
            return good

        assert heal_alias_hashes(env.engine, locate=locate) == 1
        assert read(env, item, kept).kavita_md5 is not None
        assert read(env, item, broken).kavita_md5 is None


def test_a_lazy_read_fills_one_item_only(tmp_path: Path) -> None:
    file = tmp_path / "volume.cbz"
    file.write_bytes(b"volume bytes")
    with reading_env(tmp_path) as env:
        one = env.file("ok", env.units("ok", 1))
        two = env.file("ok", env.units("ok", 1, first=2))
        h1, h2 = alias_row(env, one, b"volume bytes"), alias_row(env, two, b"volume bytes")
        assert heal_alias_hashes(env.engine, locate=lambda *_: file, export_item_id=one) == 1
        assert read(env, one, h1).kavita_md5 is not None
        assert read(env, two, h2).kavita_md5 is None
        assert new_ulid()  # (the id of an unknown item heals nothing)
        assert heal_alias_hashes(env.engine, locate=lambda *_: file, export_item_id=new_ulid()) == 0
