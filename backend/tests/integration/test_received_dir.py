"""The inbox of the follow cycle is reserved and excluded from Kavita (contradictor review, M1a)."""

import fnmatch
import io
import shutil
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.composition import RECEIVED_DIR as COMPOSED_DIR
from shelfhold.export.domain.physical_roots import (
    LEGACY_RECEIVED_DIR,
    RECEIVED_DIR,
    is_received_rel,
    is_reserved,
)
from shelfhold.export.targets.kavita.dto import HIDDEN_DIR_PATTERNS
from shelfhold.infrastructure.fsops import Fsops, PathEscapeError, ReservedPathError
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.library.public import migrate_received_dirs
from tests.fsops_env import make_env
from tests.intents_env import prepare_database

pytestmark = pytest.mark.req("EF-EXP-006", "EF-EXP-059")


def test_the_inbox_is_reserved_and_kavita_is_told_to_skip_it() -> None:
    rel = f"{RECEIVED_DIR}/01VARIANT.cbz"
    assert COMPOSED_DIR == RECEIVED_DIR == ".shelfhold/received"
    assert is_reserved(rel) and is_received_rel(rel)
    assert any(fnmatch.fnmatch(f"/lib/{rel}", pattern) for pattern in HIDDEN_DIR_PATTERNS)
    assert not is_received_rel(".shelfhold/tombstones") and not is_received_rel("a/.shelfhold/x")
    assert not is_received_rel(f"{RECEIVED_DIR}/../tombstones")


def test_fsops_writes_the_inbox_and_nothing_else_of_the_host_area(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    fsops = Fsops.open(env.path_map, env.expected)
    books = env.expected[1].path  # a root that is a path map entry, as in production
    inbox = f"{books}/{RECEIVED_DIR}"
    fsops.write_atomic(f"{inbox}/a.cbz", io.BytesIO(b"x"), parents=True)
    assert (Path(books) / RECEIVED_DIR / "a.cbz").read_bytes() == b"x"
    with pytest.raises(ReservedPathError):
        fsops.write_atomic(f"{books}/.shelfhold/tombstones", io.BytesIO(b"x"))
    with pytest.raises(PathEscapeError):  # a climb out of the inbox is refused before anything
        fsops.write_atomic(f"{inbox}/../tombstones", io.BytesIO(b"x"))


def _rows(tmp_path: Path, root_id: str, names: list[str]) -> sa.Engine:
    engine = prepare_database(tmp_path / "shelfhold.db")
    with sqlite3.connect(tmp_path / "shelfhold.db") as raw:  # no parent rows needed for this
        raw.execute("PRAGMA foreign_keys=OFF")
        for name in names:
            raw.execute(
                "INSERT INTO replica (id, unit_id, location, physical_root_id, rel_path,"
                " independent, mutable_by_third_party, fidelity)"
                " VALUES (?, ?, 'library', ?, ?, 1, 1, 'original')",
                (new_ulid(), new_ulid(), root_id, f"{LEGACY_RECEIVED_DIR}/{name}.cbz"),
            )
    return engine


def _paths(engine: sa.Engine) -> list[str]:
    with engine.connect() as connection:
        return sorted(connection.execute(sa.text("SELECT rel_path FROM replica")).scalars())


def _migrate(engine: sa.Engine, fsops: Fsops, *roots: object) -> int:
    return migrate_received_dirs(
        engine, fsops, [(r.root_id, r.path) for r in roots],  # type: ignore[attr-defined]
        old_dir=LEGACY_RECEIVED_DIR, new_dir=RECEIVED_DIR,
    )  # fmt: skip


def test_an_existing_inbox_moves_under_the_host_area_with_its_rows(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    fsops = Fsops.open(env.path_map, env.expected)
    root = env.expected[1]
    old = Path(root.path) / LEGACY_RECEIVED_DIR
    old.mkdir()
    (old / "one.cbz").write_bytes(b"1")
    (old / "two.cbz").write_bytes(b"2")
    (Path(root.path) / RECEIVED_DIR).mkdir(parents=True)
    (Path(root.path) / RECEIVED_DIR / "two.cbz").write_bytes(b"already there")
    engine = _rows(tmp_path, root.root_id, ["one", "two"])
    assert _migrate(engine, fsops, root) == 1
    assert (Path(root.path) / RECEIVED_DIR / "one.cbz").read_bytes() == b"1"
    assert (Path(root.path) / RECEIVED_DIR / "two.cbz").read_bytes() == b"already there"
    assert (old / "two.cbz").exists()  # a taken name is never replaced
    # the row of the file that moved follows it; the row of the one that stayed does not
    assert _paths(engine) == [f"{LEGACY_RECEIVED_DIR}/two.cbz", f"{RECEIVED_DIR}/one.cbz"]
    assert _migrate(engine, fsops, root) == 0  # repeatable


def test_a_legacy_inbox_that_is_a_link_is_reported_and_its_rows_stay(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    fsops = Fsops.open(env.path_map, env.expected)
    root = env.expected[1]
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "one.cbz").write_bytes(b"1")
    (Path(root.path) / LEGACY_RECEIVED_DIR).symlink_to(real)
    engine = _rows(tmp_path, root.root_id, ["one"])
    assert _migrate(engine, fsops, root) == 0
    assert _paths(engine) == [f"{LEGACY_RECEIVED_DIR}/one.cbz"]  # still where the file is
    assert (real / "one.cbz").exists()


def test_a_sub_folder_of_the_legacy_inbox_is_left_with_its_rows(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    fsops = Fsops.open(env.path_map, env.expected)
    root = env.expected[1]
    sub = Path(root.path) / LEGACY_RECEIVED_DIR / "sub"
    sub.mkdir(parents=True)
    (sub / "x.cbz").write_bytes(b"x")
    engine = _rows(tmp_path, root.root_id, ["sub/x"])
    assert _migrate(engine, fsops, root) == 0
    assert (sub / "x.cbz").exists()
    assert _paths(engine) == [f"{LEGACY_RECEIVED_DIR}/sub/x.cbz"]


def _sized_row(engine: sa.Engine, size: int) -> None:
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE replica SET size = :s"), {"s": size})


def test_a_row_left_stale_by_an_interrupted_run_is_healed(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    fsops = Fsops.open(env.path_map, env.expected)
    root = env.expected[1]
    new = Path(root.path) / RECEIVED_DIR
    new.mkdir(parents=True)
    (new / "one.cbz").write_bytes(b"1")  # moved, but the row was not written
    engine = _rows(tmp_path, root.root_id, ["one"])
    _sized_row(engine, 1)
    _migrate(engine, fsops, root)
    assert _paths(engine) == [f"{RECEIVED_DIR}/one.cbz"]


def test_a_row_is_never_pointed_at_another_file_of_the_same_name(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    fsops = Fsops.open(env.path_map, env.expected)
    root = env.expected[1]
    new = Path(root.path) / RECEIVED_DIR
    new.mkdir(parents=True)
    (new / "one.cbz").write_bytes(b"some other file")  # same name, not the one the row is about
    engine = _rows(tmp_path, root.root_id, ["one"])
    _sized_row(engine, 1)
    _migrate(engine, fsops, root)
    assert _paths(engine) == [f"{LEGACY_RECEIVED_DIR}/one.cbz"]


def test_a_root_that_owes_tombstone_lines_is_not_migrated(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    fsops = Fsops.open(env.path_map, env.expected)
    root = env.expected[1]
    old = Path(root.path) / LEGACY_RECEIVED_DIR
    old.mkdir()
    (old / "one.cbz").write_bytes(b"1")
    engine = _rows(tmp_path, root.root_id, ["one"])
    with sqlite3.connect(tmp_path / "shelfhold.db") as raw:  # no parent rows needed for this
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute(
            "INSERT INTO tombstone_pending_root (root_id, seq, line) VALUES (?, 1, 'x')",
            (root.root_id,),
        )
    assert _migrate(engine, fsops, root) == 0
    assert (old / "one.cbz").exists()


def test_a_root_that_is_away_does_not_stop_the_others(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    fsops = Fsops.open(env.path_map, env.expected)
    here, away = env.expected[0], env.expected[1]  # `books` sits inside `library`
    old = Path(here.path) / LEGACY_RECEIVED_DIR
    old.mkdir()
    (old / "one.cbz").write_bytes(b"1")
    shutil.rmtree(away.path)
    engine = _rows(tmp_path, here.root_id, ["one"])
    assert _migrate(engine, fsops, away, here) == 1  # no exception, the other root is done
    assert _paths(engine) == [f"{RECEIVED_DIR}/one.cbz"]


def test_the_inbox_folder_itself_cannot_be_moved_or_purged(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    fsops = Fsops.open(env.path_map, env.expected)
    books = env.expected[1].path
    inbox = f"{books}/{RECEIVED_DIR}"
    fsops.write_atomic(f"{inbox}/a.cbz", io.BytesIO(b"x"), parents=True)
    assert not is_received_rel(RECEIVED_DIR)
    with pytest.raises(ReservedPathError):
        fsops.rename(inbox, f"{books}/moved")
    with pytest.raises(ReservedPathError):
        fsops.purge_remove_tree(inbox)
    with pytest.raises(ReservedPathError):
        fsops.mkdir(inbox)
    assert (Path(books) / RECEIVED_DIR / "a.cbz").exists()


def test_a_new_inbox_that_is_a_link_lets_nothing_out(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    fsops = Fsops.open(env.path_map, env.expected)
    root = env.expected[1]
    outside = tmp_path / "outside"
    outside.mkdir()
    (Path(root.path) / ".shelfhold").mkdir(exist_ok=True)
    (Path(root.path) / RECEIVED_DIR).symlink_to(outside)
    old = Path(root.path) / LEGACY_RECEIVED_DIR
    old.mkdir()
    (old / "one.cbz").write_bytes(b"1")
    engine = _rows(tmp_path, root.root_id, ["one"])
    _migrate(engine, fsops, root)  # reported and skipped, never raised
    assert not list(outside.iterdir())
