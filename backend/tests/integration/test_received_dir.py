"""The inbox of the follow cycle is reserved and excluded from Kavita (contradictor review, M1a)."""

import fnmatch
import io
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
    fsops.mkdir(inbox)
    fsops.write_atomic(f"{inbox}/a.cbz", io.BytesIO(b"x"), parents=True)
    assert (Path(books) / RECEIVED_DIR / "a.cbz").read_bytes() == b"x"
    with pytest.raises(ReservedPathError):
        fsops.write_atomic(f"{books}/.shelfhold/tombstones", io.BytesIO(b"x"))
    with pytest.raises(PathEscapeError):  # a climb out of the inbox is refused before anything
        fsops.write_atomic(f"{inbox}/../tombstones", io.BytesIO(b"x"))


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
    engine = prepare_database(tmp_path / "shelfhold.db")
    with sqlite3.connect(tmp_path / "shelfhold.db") as raw:  # no parent rows needed for this
        raw.execute("PRAGMA foreign_keys=OFF")
        for name in ("one", "two"):
            raw.execute(
                "INSERT INTO replica (id, unit_id, location, physical_root_id, rel_path,"
                " independent, mutable_by_third_party, fidelity)"
                " VALUES (?, ?, 'library', ?, ?, 1, 1, 'original')",
                (new_ulid(), new_ulid(), root.root_id, f"{LEGACY_RECEIVED_DIR}/{name}.cbz"),
            )
    moved = migrate_received_dirs(
        engine, fsops, [(root.root_id, root.path)],
        old_dir=LEGACY_RECEIVED_DIR, new_dir=RECEIVED_DIR,
    )  # fmt: skip
    assert moved == 1
    assert (Path(root.path) / RECEIVED_DIR / "one.cbz").read_bytes() == b"1"
    assert (Path(root.path) / RECEIVED_DIR / "two.cbz").read_bytes() == b"already there"
    assert (old / "two.cbz").exists()  # a taken name is never replaced
    with engine.connect() as connection:
        paths = set(connection.execute(sa.text("SELECT rel_path FROM replica")).scalars())
    assert paths == {f"{RECEIVED_DIR}/one.cbz", f"{RECEIVED_DIR}/two.cbz"}
    again = migrate_received_dirs(
        engine, fsops, [(root.root_id, root.path)],
        old_dir=LEGACY_RECEIVED_DIR, new_dir=RECEIVED_DIR,
    )  # fmt: skip
    assert again == 0  # repeatable
