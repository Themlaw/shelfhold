"""A forged or damaged mirror never takes an export down (EF-EXP-063, GAP-9-10, PR #50 review)."""

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.export.domain.physical_roots import RESERVED_DIR
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import platform
from tests.export_env import ExportEnv, export_env
from tests.mirror_env import entry, header, raw

pytestmark = pytest.mark.req("EF-EXP-063")


def mirror_file(env: ExportEnv) -> Path:
    (path,) = sorted((env.lib.library / RESERVED_DIR / "manifest").rglob("*.jsonl"))
    return path


def nested(depth: int) -> bytes:
    return b'{"kind":"entry","x":' + b"[" * depth + b"]" * depth + b"}\n"


def deep_json(path: Path) -> None:
    path.write_bytes(path.read_bytes() + nested(100_000))


def duplicate_keys(path: Path) -> None:
    path.write_bytes(path.read_bytes() + b'{"kind":"entry","rel_path":"a","rel_path":"b"}\n')


def too_many_lines(path: Path) -> None:
    path.write_bytes(path.read_bytes() + b'{"kind":"entry"}\n' * 25_000)


def unhashable_identifier(path: Path) -> None:
    bad = entry("Dr Stone/forged.cbz", export_item={"id": [1, 2]})
    path.write_bytes(path.read_bytes() + raw(bad))


def not_json(path: Path) -> None:
    path.write_bytes(b"\xff\xfe" + b"\x00" * 64)


# (forgery, unreadable as a whole): a bad line is kept aside verbatim and the file still updated;
# a file that cannot be read at all is left untouched and reported.
FORGERIES: list[tuple[Callable[[Path], None], bool]] = [
    (deep_json, False), (duplicate_keys, False), (unhashable_identifier, False),
    (too_many_lines, True), (not_json, True),
]  # fmt: skip


def events(env: ExportEnv) -> list[str]:
    with env.engine.connect() as connection:
        return list(connection.execute(sa.select(platform.event.c.type)).scalars())


@pytest.mark.parametrize(
    ("forge", "unreadable"), FORGERIES, ids=lambda f: getattr(f, "__name__", "")
)
def test_a_forged_mirror_never_stops_the_next_export(
    forge: Callable[[Path], None], unreadable: bool
) -> None:
    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        path = mirror_file(env)
        forge(path)
        forged = path.read_bytes()
        env.add_chapter(series, "2")
        env.pipeline.run(env.destination_id, job_id=new_ulid())  # does not raise
        assert sorted(p.name for p in env.files()) == ["Dr Stone - Ch.1.cbz", "Dr Stone - Ch.2.cbz"]
        if unreadable:
            assert env.pipeline.mirror.skipped and "reconcile.drift" in events(env)
            assert path.read_bytes() == forged  # a file we cannot read is never overwritten
        else:
            text = path.read_bytes().decode()
            assert "Ch.2" in text  # the mirror went on
            assert forged.splitlines()[-1].decode() in text  # the bad line is kept, not erased


def test_a_symlink_in_place_of_the_mirror_file_is_ignored_and_reported() -> None:
    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        path = mirror_file(env)
        target = env.lib.tmp / "elsewhere.jsonl"
        target.write_text("outside")
        path.unlink()
        path.symlink_to(target)
        env.add_chapter(series, "2")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert len(env.files()) == 2
        assert target.read_text() == "outside"  # nothing was written through the link
        assert env.pipeline.mirror.skipped and "reconcile.drift" in events(env)


def test_a_symlink_in_place_of_the_mirror_folder_is_ignored_and_reported() -> None:
    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        folder = mirror_file(env).parent
        outside = env.lib.tmp / "outside"
        shutil.copytree(folder, outside)
        shutil.rmtree(folder)
        folder.symlink_to(outside, target_is_directory=True)
        before = sorted(p.name for p in outside.iterdir())
        env.add_chapter(series, "2")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert len(env.files()) == 2
        assert sorted(p.name for p in outside.iterdir()) == before
        assert env.pipeline.mirror.skipped and "reconcile.drift" in events(env)


def test_a_failing_mirror_write_does_not_fail_the_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    with export_env() as env:

        def broken(self: Fsops, root_path: str, name: str, data: bytes) -> None:
            raise OSError("disk says no")

        monkeypatch.setattr(Fsops, "write_mirror", broken)
        series = env.new_series()
        env.add_chapter(series, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())  # the file is placed and kept
        assert len(env.files()) == 1
        monkeypatch.undo()
        env.pipeline.heal_mirror(env.destination_id)  # the next window heals it
        assert mirror_file(env).exists()


def test_a_valid_older_file_is_still_merged_after_a_forgery_is_removed() -> None:
    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        path = mirror_file(env)
        good = path.read_bytes()
        path.write_bytes(good + raw(entry("Dr Stone/old.cbz", state="unresolved")))
        assert json.loads(good.splitlines()[0])["kind"] == "header"
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert b"old.cbz" in path.read_bytes()
        assert header()["kind"] == "header"
