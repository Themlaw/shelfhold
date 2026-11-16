"""EF-EXP-010 (M4 L07, intouchable : intégrité): the guard against the local source of Suwayomi.

A native folder bound to the folder Suwayomi reads as its local source would be read back by
Shelfhold as an upstream: it would acquire its own exports, forever. So the guard runs in both
directions and never lets the two meet: a local source on a root Shelfhold manages (the root of a
destination, `/store`, the library) is refused, and so is a native folder root that covers the local
source. Parents, children, links, `..`, another case, another Unicode form and a bind mount
(`st_dev`, `st_ino`) are the same folder. The check is made again at every follow cycle: a Suwayomi
that moves its local source onto a managed root pauses the follow with a typed code and nothing is
deleted.
"""

import os
import unicodedata
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import FolderFacts, RootGuard
from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.public import LocalSourceUnknownError, RootOverlapsLocalSourceError
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, platform
from shelfhold.plugin_host.errors import LocalSourceOnManagedRootError
from tests.export_env import ExportEnv, export_env
from tests.fakes.suwayomi import FakeSuwayomi
from tests.fan_out_env import add_target
from tests.follow_env import follow_bench


def _suwayomi_source(env: ExportEnv, path: str | None = None, *, seen: bool = True) -> None:
    """A Suwayomi source is configured; `path` is what its last cycle read (None: never read)."""
    with write_transaction(env.engine) as connection:
        connection.execute(sa.insert(acquisition.source_config).values(plugin_id="suwayomi"))
        if seen:
            RootGuard().record(connection, "suwayomi", path)


def _managed(env: ExportEnv, base: Path) -> Path:
    """A root of a destination that no other root contains: the disguises are tried against it."""
    root = base / "managed" / "Dest"
    root.mkdir(parents=True)
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.insert(platform.physical_root).values(
                root_id=new_ulid(), path_entry=str(root), mode="rw"
            )
        )
    return root


def _native_target(env: ExportEnv) -> str:
    return add_target(env, kind="native_folder", partitioning="none")


def _bind(env: ExportEnv, subpath: str, target_id: str) -> str:
    (env.lib.manga / subpath).mkdir(parents=True, exist_ok=True)
    return ShelfService(
        env.engine, clock=env.clock, root_check=RootGuard().check_root
    ).bind_destination(
        shelf_id=env.shelf_id, target_id=target_id, transport="tree", plugin_egress="foyer",
        root_subpath=subpath,
    )  # fmt: skip


# -- a local source of Suwayomi on a root Shelfhold manages -------------------------------------


@pytest.mark.req("EF-EXP-010")
def test_suwayomi_local_source_on_a_managed_root_refused(tmp_path: Path) -> None:
    with export_env() as env:
        lib = env.lib
        # the root of a destination is managed (the library and `/store` too)
        managed = {
            "root of a destination": lib.manga / "Kavita",
            "the library": lib.library,
            "the store": lib.store,
            "below the store": lib.store / "any" / "depth",
            "above the library": lib.library.parent,
            "the path map of a target": lib.manga,
        }
        guard = RootGuard()
        for what, path in managed.items():
            with env.engine.connect() as connection, pytest.raises(LocalSourceOnManagedRootError):
                guard.check_local_source(connection, str(path))
            assert what  # the label shows in the report of a failure
        # a folder that nothing manages is fine, whatever the depth
        free = tmp_path / "suwayomi-local" / "deep"
        free.mkdir(parents=True)
        with env.engine.connect() as connection:
            guard.check_local_source(connection, str(free))


@pytest.mark.req("EF-EXP-010")
def test_the_guard_sees_one_folder_through_every_disguise(tmp_path: Path) -> None:
    with export_env() as env:
        guard = RootGuard()
        root = _managed(env, tmp_path)
        accent = tmp_path / "managed" / "Série é"
        accent.mkdir()
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.insert(platform.physical_root).values(
                    root_id=new_ulid(), path_entry=str(accent), mode="rw"
                )
            )
        link = tmp_path / "alias"
        link.symlink_to(root)
        disguises = {
            "symbolic link": str(link),
            "below a link": str(link / "x"),
            "dot dot": f"{root}/../Dest",
            "dot dot above": f"{root.parent}/Dest/x/..",
            "another case": str(tmp_path / "MANAGED" / "dest"),
            "trailing dot": f"{root}.",
            "trailing space": f"{root} ",
            "decomposed Unicode": unicodedata.normalize("NFD", str(accent)),
            "compatibility form": str(accent).replace("é", "e\u0301"),
            "a child": str(root / "a" / "b"),
            "the parent": str(root.parent),
        }
        for what, path in disguises.items():
            with env.engine.connect() as connection, pytest.raises(LocalSourceOnManagedRootError):
                guard.check_local_source(connection, path)
            assert what

        # a neighbour that only shares a prefix is not an overlap (components, never a prefix)
        neighbour = tmp_path / "managed" / "DestOther"
        neighbour.mkdir()
        with env.engine.connect() as connection:
            guard.check_local_source(connection, str(neighbour))


@pytest.mark.req("EF-EXP-010")
def test_the_guard_sees_a_bind_mount_by_device_and_inode(tmp_path: Path) -> None:
    """Two paths that no string relates, one folder on the disk: `(st_dev, st_ino)` says so."""
    with export_env() as env:
        root = _managed(env, tmp_path)
        far = tmp_path / "elsewhere" / "mounted"
        far.mkdir(parents=True)

        def facts(path: str) -> FolderFacts:
            real = os.path.realpath(path)
            if real == str(far):  # as a bind mount shows it: the inode of the managed root
                stat = root.stat()
                identity = (stat.st_dev, stat.st_ino)
                return FolderFacts(real, identity, frozenset({identity}))
            return RootGuard().facts(path)

        with env.engine.connect() as connection:
            with pytest.raises(LocalSourceOnManagedRootError):
                RootGuard(facts=facts).check_local_source(connection, str(far))
            RootGuard().check_local_source(connection, str(far))  # the plain disk sees two folders


# -- a native folder root that covers the local source of Suwayomi ------------------------------


@pytest.mark.req("EF-EXP-010")
def test_native_folder_root_overlapping_suwayomi_local_source_refused(tmp_path: Path) -> None:
    with export_env() as env:
        local = tmp_path / "suwayomi" / "local"
        local.mkdir(parents=True)
        env.lib.manga.joinpath("local").mkdir()
        target = _native_target(env)
        # the library holds a local source folder of Suwayomi: the native root may not be it, nor
        # hold it, nor sit inside it
        _suwayomi_source(env, str(env.lib.manga / "local"))
        for subpath in ("local", "local/series"):
            with pytest.raises(RootOverlapsLocalSourceError):
                _bind(env, subpath, target)
        # a sibling is fine
        assert _bind(env, "native", target)
        with env.engine.connect() as connection:
            count = connection.execute(sa.text("SELECT count(*) FROM shelf_destination")).scalar()
        assert count == 2  # the Kavita one and the sibling: the refused ones left nothing


@pytest.mark.req("EF-EXP-010")
def test_a_native_root_that_holds_the_local_source_is_refused(tmp_path: Path) -> None:
    with export_env() as env:
        (env.lib.manga / "all" / "local").mkdir(parents=True)
        _suwayomi_source(env, str(env.lib.manga / "all" / "local"))
        with pytest.raises(RootOverlapsLocalSourceError):
            _bind(env, "all", _native_target(env))  # the parent of the local source


@pytest.mark.req("EF-EXP-010")
def test_a_suwayomi_that_was_never_read_binds_nothing_until_it_is() -> None:
    """Fail closed: a Suwayomi is configured and the follow never read its local source."""
    with export_env() as env:
        _suwayomi_source(env, seen=False)
        with pytest.raises(LocalSourceUnknownError):
            _bind(env, "native", _native_target(env))
        with write_transaction(env.engine) as connection:  # a cycle read it: no local source
            RootGuard().record(connection, "suwayomi", None)
        assert _bind(env, "native", _native_target(env))


@pytest.mark.req("EF-EXP-010")
def test_without_a_suwayomi_the_native_folder_binds_freely() -> None:
    with export_env() as env:
        assert _bind(env, "native", _native_target(env))


# -- the cycle looks again: a local source moved onto a managed root ------------------------------


COUNTS = (
    "SELECT (SELECT count(*) FROM download_request) AS tickets,"
    " (SELECT count(*) FROM replica) AS replicas,"
    " (SELECT count(*) FROM series_follow) AS follows,"
    " (SELECT count(*) FROM source_binding WHERE active) AS active,"
    " (SELECT count(*) FROM export_record) AS records,"
    " (SELECT count(*) FROM shelf_destination) AS destinations"
)


@pytest.mark.req("EF-EXP-010")
def test_suwayomi_local_path_moved_onto_managed_root_pauses_binding(tmp_path: Path) -> None:
    server = FakeSuwayomi(auth_mode="none")
    free = tmp_path / "suwayomi-local"
    free.mkdir()
    server.state["local_source_path"] = str(free)
    with follow_bench(server) as bench:
        bench.cycle = bench.new_cycle(root_guard=RootGuard())
        assert bench.run().paused is None  # a free folder: followed as before
        recorded = bench.rows("SELECT health FROM source_config")[0]["health"]
        assert str(free) in recorded  # what the cycle read, for the binding of a native folder
        before = bench.rows(COUNTS)[0]
        files = bench.library_files()

        # Suwayomi is set to read a root Shelfhold manages: the follow pauses, with a typed code
        server.state["local_source_path"] = str(bench.lib.store)
        report = bench.run()
        assert report.paused == "local_source_loop"
        assert [e["reason"] for e in bench.events("source.broken")] == [
            "local_source_on_managed_root"
        ]
        assert bench.rows(COUNTS)[0] == before  # nothing deleted, nothing asked again
        assert bench.library_files() == files and bench.binding()["active"]

        sent = len(server.requests)
        assert bench.run().paused == "local_source_loop" and len(server.requests) == sent
        assert len(bench.events("source.broken")) == 1  # told once

        # the admin fixes the setting of Suwayomi and resumes: the follow goes on
        server.state["local_source_path"] = str(free)
        bench.cycle.resume()
        assert bench.run().paused is None
        assert bench.rows(COUNTS)[0]["active"] == before["active"]

        # resumed without fixing anything: the next cycle reads the folder again and pauses again
        server.state["local_source_path"] = str(bench.lib.store)
        bench.cycle.resume()
        assert bench.run().paused == "local_source_loop"


@pytest.mark.req("EF-EXP-010")
def test_a_local_source_that_is_not_exposed_pauses_nothing(tmp_path: Path) -> None:
    server = FakeSuwayomi(auth_mode="none")
    server.state["local_source_path"] = None  # a server that does not give it
    with follow_bench(server) as bench:
        bench.cycle = bench.new_cycle(root_guard=RootGuard())
        assert bench.run().paused is None


@pytest.mark.req("EF-EXP-010")
@pytest.mark.parametrize("answer", [None, "relative/path", "/with/nul\u0000byte", 42])
def test_an_unreadable_answer_is_unknown_never_a_free_binding(
    tmp_path: Path, answer: object
) -> None:
    """A path that was read once, then an answer that cannot be read: unknown, so closed."""
    server = FakeSuwayomi(auth_mode="none")
    free = tmp_path / "suwayomi-local"
    free.mkdir()
    server.state["local_source_path"] = str(free)
    with follow_bench(server) as bench:
        bench.cycle = bench.new_cycle(root_guard=RootGuard())
        assert bench.run().paused is None
        server.state["local_source_path"] = answer
        assert bench.run().paused is None  # nothing proves a loop: the follow goes on
        with bench.lib.engine.connect() as connection:
            assert RootGuard.known(connection) == [None]  # forgotten, not "no local source"
            with pytest.raises(LocalSourceUnknownError):
                RootGuard().check_root(connection, str(tmp_path / "somewhere"))
        server.state["local_source_path"] = ""  # the server says it has none of its own: read
        assert bench.run().paused is None
        with bench.lib.engine.connect() as connection:
            assert RootGuard.known(connection) == []
            RootGuard().check_root(connection, str(tmp_path / "somewhere"))


@pytest.mark.req("EF-EXP-010")
def test_a_local_source_that_no_path_map_translates_cannot_be_told_apart(tmp_path: Path) -> None:
    """Suwayomi reports a path of its own container: it does not exist here and no `remote` covers
    it. Nothing proves it apart from a managed root: the native folder is refused, not believed."""
    with export_env() as env:
        _suwayomi_source(env, "/home/suwayomi/.local/share/local")
        with pytest.raises(LocalSourceUnknownError):
            _bind(env, "native", _native_target(env))
        with env.engine.connect() as connection:  # the cycle itself does not pause for it
            RootGuard().check_local_source(connection, "/home/suwayomi/.local/share/local")


@pytest.mark.req("EF-EXP-010")
def test_the_remote_side_of_a_path_map_is_compared_both_ways(tmp_path: Path) -> None:
    with export_env() as env:
        target = _native_target(env)  # local = the library, remote = /manga
        guard = RootGuard()
        with env.engine.connect() as connection:
            for reported in ("/manga", "/manga/local", "/MANGA/Local/deep"):  # holds, is, inside
                with pytest.raises(LocalSourceOnManagedRootError):
                    guard.check_local_source(connection, reported)
            guard.check_local_source(connection, "/elsewhere/local")  # nothing to translate
        _suwayomi_source(env, "/manga/Own")  # the folder a reader sees as the one of the binding
        with pytest.raises(RootOverlapsLocalSourceError):
            _bind(env, "Own", target)
        assert _bind(env, "other", target)  # a sibling the path map translates: proven apart
