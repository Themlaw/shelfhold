"""EF-EXP-015 (M2 L18): the hard link is an option of one road, for replaceable content on one
mount. Every "never" of the rule is attacked here: an irreplaceable Unit, a safeguard, a kept copy
that is not the option's, another mount, a symbolic link, a file changed through the other name."""

import errno
import hashlib
import os
import stat
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import structlog

from shelfhold.export.application.shelves import ShelfService
from shelfhold.infrastructure.fsops import LinkUnavailableError
from shelfhold.infrastructure.fsops import writer as fs_writer
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export, storage
from shelfhold.library.public import KeepCopier, LinkPolicy, independent_copies, verify_kept
from tests.integration.test_library_keep_copy import ingest, keeper, prepare
from tests.library_env import LibEnv, cbz_bytes, lib_env
from tests.locks_env import work_lock

pytestmark = pytest.mark.req("EF-EXP-015")


def linking(env: LibEnv) -> KeepCopier:
    """The keeper of the catch-up job: the only one given the option."""
    from shelfhold.export.application.intents import IntentJournal

    return KeepCopier(
        env.engine, env.fsops, env.locate, str(env.store), env.data,
        IntentJournal(env.engine, env.fsops), hold_work=work_lock(env.engine),
        links=LinkPolicy(env.engine),
    )  # fmt: skip


def shelf_mode(env: LibEnv, mode: str) -> str:
    shelf_id = new_ulid()
    with env.engine.begin() as c:
        c.execute(
            sa.insert(export.shelf).values(
                id=shelf_id, name="S", phrase="p", retention_mode="keep", link_mode=mode
            )
        )
        c.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == env.chain["edition"])
            .values(shelf_id=shelf_id)
        )
    return shelf_id


def filed(env: LibEnv, mode: str = "hardlink") -> tuple[str, Path, bytes]:
    prepare(env)
    shelf_mode(env, mode)
    data = cbz_bytes()
    unit = env.chain["unit"]
    return unit, ingest(env, data, unit, env.chain["variant"], "Dr Stone - 1.cbz"), data


def stored(env: LibEnv) -> Path:
    (path,) = env.files_under(env.store)
    return path


def make_irreplaceable(env: LibEnv) -> None:
    with env.engine.begin() as c:
        c.execute(sa.update(catalog.unit).values(availability="removed"))


def test_import_mode_copy_default_hardlink_option_same_fs() -> None:
    with lib_env() as env:
        unit, library, data = filed(env, "copy")
        kept = linking(env).keep(unit)  # the option is off on this shelf: the default holds
        assert kept.method in {"reflink", "copy"}
        assert stored(env).stat().st_ino != library.stat().st_ino
    with lib_env() as env:
        unit, library, data = filed(env)
        kept = linking(env).keep(unit)
        path = stored(env)
        assert kept.method == "hardlink"
        assert path.read_bytes() == data
        assert (path.stat().st_ino, path.stat().st_nlink) == (library.stat().st_ino, 2)
        assert not path.is_symlink() and stat.S_ISREG(path.stat().st_mode)
        with env.engine.connect() as c:
            row = c.execute(sa.select(storage.content)).one()
            assert row.retention == "keep"
            # one inode under two names is no second copy: the Health page does not count it
            assert independent_copies(c, unit) == 1
        assert verify_kept(env.engine, env.fsops, unit) == 0


def test_the_default_of_a_shelf_is_copy() -> None:
    with lib_env() as env:
        unit, library, _ = filed(env, "copy")
        with env.engine.connect() as c:
            assert LinkPolicy(env.engine).allows(unit) is False
            assert c.execute(sa.select(export.shelf.c.link_mode)).scalar_one() == "copy"
        # no shelf at all: the default
        with env.engine.begin() as c:
            c.execute(sa.update(catalog.edition).values(shelf_id=None))
        assert LinkPolicy(env.engine).allows(unit) is False


def test_hardlink_across_mounts_falls_back_to_copy_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with lib_env() as env:
        unit, library, data = filed(env)
        monkeypatch.setattr(fs_writer, "device_of_root", lambda root: id(root) % 100000)
        with structlog.testing.capture_logs() as logs:
            kept = linking(env).keep(unit)
        assert kept.method in {"reflink", "copy"}
        assert stored(env).stat().st_ino != library.stat().st_ino
        assert stored(env).read_bytes() == data
        warned = [e for e in logs if e["event"] == "store.link_fallback_copy"]
        assert warned and warned[0]["reason"] == "cross_mount"
        assert warned[0]["log_level"] == "warning"


def test_exdev_from_the_kernel_is_a_mount_boundary_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bind mount of the same file system has the same `st_dev` and still refuses a link."""
    with lib_env() as env:
        unit, library, _ = filed(env)

        def refuse(*a: Any, **k: Any) -> None:
            raise OSError(errno.EXDEV, "cross-device")

        monkeypatch.setattr(os, "link", refuse)
        with structlog.testing.capture_logs() as logs:
            kept = linking(env).keep(unit)
        assert kept.method in {"reflink", "copy"}
        assert stored(env).stat().st_ino != library.stat().st_ino
        assert any(e["event"] == "store.link_fallback_copy" for e in logs)


@pytest.mark.parametrize("how", ["removed", "no_binding"])
def test_irreplaceable_unit_never_hardlinked(how: str) -> None:
    with lib_env() as env:
        unit, library, data = filed(env)
        if how == "removed":
            make_irreplaceable(env)
        else:
            with env.engine.begin() as c:
                c.execute(sa.text("UPDATE source_binding SET active = 0"))
        assert LinkPolicy(env.engine).allows(unit) is False
        kept = linking(env).keep(unit)
        assert kept.method in {"reflink", "copy"}
        assert stored(env).stat().st_ino != library.stat().st_ino
        assert stored(env).stat().st_nlink == 1 and library.stat().st_nlink == 1
        with env.engine.connect() as c:
            assert independent_copies(c, unit) == 2


def test_a_keeper_without_the_option_never_links_whatever_the_shelf_says() -> None:
    """The safeguard ("Mettre à l'abri") and the plain keep are built without the option."""
    with lib_env() as env:
        unit, library, _ = filed(env)
        keeper(env).keep(unit)
        assert stored(env).stat().st_ino != library.stat().st_ino
        assert stored(env).stat().st_nlink == 1


def test_unit_that_became_irreplaceable_gets_an_independent_copy_from_the_safeguard() -> None:
    with lib_env() as env:
        unit, library, data = filed(env)
        linking(env).keep(unit)
        assert stored(env).stat().st_nlink == 2
        make_irreplaceable(env)
        keeper(env).keep(unit)  # the safeguard path: no option, and it replaces by rename
        assert stored(env).stat().st_ino != library.stat().st_ino
        assert stored(env).stat().st_nlink == 1 and library.stat().st_nlink == 1
        assert stored(env).read_bytes() == data == library.read_bytes()
        assert verify_kept(env.engine, env.fsops, unit) == 1
        with env.engine.connect() as c:
            assert independent_copies(c, unit) == 2


def test_never_symlink_the_source_is_never_followed(tmp_path: Path) -> None:
    with lib_env() as env:
        unit, library, data = filed(env)
        outside = tmp_path / "outside.cbz"
        outside.write_bytes(data)
        library.unlink()
        library.symlink_to(outside)
        with pytest.raises(Exception) as refused:  # noqa: PT011 - a link in a managed tree
            env.fsops.link_replaceable(
                str(library), str(env.store / "x.cbz"), expected_sha256=hashlib.sha256(data).hexdigest()
            )
        assert not isinstance(refused.value, FileNotFoundError)
        assert not (env.store / "x.cbz").exists()
        assert not os.path.lexists(env.store / "x.cbz")


def test_never_symlink_nothing_in_the_source_creates_one() -> None:
    import ast

    src = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
    offenders = []
    for path in src.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr in {"symlink", "symlink_to"}:
                offenders.append(f"{path.relative_to(src)}:{node.lineno}")
    assert offenders == []


def test_a_name_shared_with_a_third_party_is_not_linked_again() -> None:
    """`nlink` is read before anything is placed: a file with a name the user made is copied."""
    with lib_env() as env:
        unit, library, _ = filed(env)
        os.link(library, env.tmp / "users-own-name.cbz")
        with structlog.testing.capture_logs() as logs:
            kept = linking(env).keep(unit)
        assert kept.method in {"reflink", "copy"}
        assert stored(env).stat().st_ino != library.stat().st_ino
        assert any(e.get("reason") == "shared_name" for e in logs)


def test_the_link_is_checked_after_it_is_made() -> None:
    with lib_env() as env:
        unit, library, data = filed(env)
        done = env.fsops.link_replaceable(
            str(library), str(env.store / "units" / "a.cbz"), parents=True,
            expected_sha256=hashlib.sha256(data).hexdigest(),
        )  # fmt: skip
        assert done.nlink == 2 and library.stat().st_nlink == 2


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EDQUOT])
def test_full_disk_or_quota_leaves_nothing_and_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    with lib_env() as env:
        unit, library, _ = filed(env)

        def full(*a: Any, **k: Any) -> None:
            raise OSError(code, "no room for a name")

        monkeypatch.setattr(os, "link", full)
        with pytest.raises(OSError) as raised:
            linking(env).keep(unit)
        assert raised.value.errno == code  # a copy would not fit either: said, not hidden
        assert env.files_under(env.store) == []
        assert list((env.store / ".shelfhold" / "staging").glob("*")) == []
        with env.engine.connect() as c:
            assert c.execute(sa.select(storage.content.c.retention)).scalar_one() == "passthrough"
        assert library.stat().st_nlink == 1


def test_modification_through_the_other_name_is_seen_by_the_integrity_checks() -> None:
    """Kavita or the user writes the library file in place: both names change, and the store
    copy stops counting (hash read now against the hash stored)."""
    with lib_env() as env:
        unit, library, data = filed(env)
        linking(env).keep(unit)
        with library.open("r+b") as stream:
            stream.write(b"PK-damaged")
        path = stored(env)
        assert path.read_bytes() == library.read_bytes() != data  # one inode, one content
        served = env.fsops.sha256_of(str(library))
        with env.engine.connect() as c:
            original = c.execute(sa.select(storage.content.c.original_sha256)).scalar_one()
        assert served != original  # the file_hash of what is served no longer matches
        assert verify_kept(env.engine, env.fsops, unit) == 0
        with env.engine.connect() as c:
            assert independent_copies(c, unit) == 0


def test_purge_of_one_side_leaves_the_other_whole() -> None:
    for side in ("library", "store"):
        with lib_env() as env:
            unit, library, data = filed(env)
            linking(env).keep(unit)
            path = stored(env)
            gone, left = (library, path) if side == "library" else (path, library)
            env.fsops.purge_unlink(str(gone))
            assert not gone.exists()
            assert left.read_bytes() == data and left.stat().st_nlink == 1


def test_restoration_from_the_store_is_an_independent_copy() -> None:
    with lib_env() as env:
        unit, library, data = filed(env)
        linking(env).keep(unit)
        path = stored(env)
        env.fsops.purge_unlink(str(library))
        env.fsops.copy_independent(str(path), str(library), parents=True)
        assert library.read_bytes() == data
        assert library.stat().st_ino != path.stat().st_ino
        assert path.stat().st_nlink == 1 == library.stat().st_nlink


def test_replacing_a_linked_name_goes_by_rename_and_leaves_the_other_name() -> None:
    with lib_env() as env:
        unit, library, data = filed(env)
        linking(env).keep(unit)
        path = stored(env)
        inode = library.stat().st_ino
        env.fsops.write_atomic(str(path), b"new store bytes", replace=True)
        assert library.read_bytes() == data and library.stat().st_ino == inode
        assert library.stat().st_nlink == 1  # the store name left the inode
        assert path.read_bytes() == b"new store bytes"


def test_link_replaceable_refuses_a_target_that_is_not_regular_or_missing() -> None:
    with lib_env() as env:
        unit, library, data = filed(env)
        with pytest.raises((LinkUnavailableError, OSError)):
            env.fsops.link_replaceable(
                str(env.library / "manga"), str(env.store / "d.cbz"), expected_sha256="0" * 64
            )
        with pytest.raises(Exception):  # noqa: B017, PT011 - the digest read now is not the one given
            env.fsops.link_replaceable(
                str(library), str(env.store / "e.cbz"), expected_sha256="0" * 64
            )
        assert not (env.store / "e.cbz").exists() and library.stat().st_nlink == 1


def test_shelf_link_mode_has_one_audited_writer_and_never_accepts_symlink() -> None:
    with lib_env() as env:
        shelf_id = shelf_mode(env, "copy")
        service = ShelfService(env.engine)
        service.set_link_mode(shelf_id, "hardlink")
        with env.engine.connect() as c:
            assert c.execute(sa.select(export.shelf.c.link_mode)).scalar_one() == "hardlink"
        for bad in ("symlink", "reflink", ""):
            with pytest.raises(ValueError):
                service.set_link_mode(shelf_id, bad)
        with pytest.raises(sa.exc.IntegrityError), env.engine.begin() as c:
            c.execute(sa.update(export.shelf).values(link_mode="symlink"))
        with pytest.raises(LookupError):
            service.set_link_mode(new_ulid(), "copy")


def test_cli_shelf_link_mode(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from contextlib import contextmanager

    from shelfhold.interfaces import cli
    from shelfhold.interfaces.cli import _ops
    from shelfhold.interfaces.local_cli.ops import OPS_METHODS

    calls: list[tuple[str, dict[str, Any]]] = []

    class Channel:
        def call(self, method: str, **params: Any) -> dict[str, Any]:
            calls.append((method, params))
            return {"shelf_id": "x", "link_mode": params["link_mode"]}

    @contextmanager
    def channel(settings: Any):  # type: ignore[no-untyped-def]
        yield Channel()

    monkeypatch.setattr(_ops, "ops_channel", channel)
    assert cli.main(["shelf", "link-mode", "Manga", "--link-mode", "hardlink"]) == 0
    assert calls == [("ops.shelf_link_mode", {"shelf": "Manga", "link_mode": "hardlink"})]
    assert "Manga" in capsys.readouterr().out
    with pytest.raises(SystemExit):  # argparse refuses anything but the two modes
        cli.main(["shelf", "link-mode", "Manga", "--link-mode", "symlink"])
    assert OPS_METHODS["ops.shelf_link_mode"][1] is None  # not a deletion: no reserved action
