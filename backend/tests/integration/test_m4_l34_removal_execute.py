"""The order and the execution of a removal and of a full detachment (M4 L34, EF-EXP-031,
EF-BIB-045, EF-EXP-041, EF-USR-024).

The queue never carries a secret and never decides: the **order** is given at the door, once, by a
fresh admin session with the phrase typed (and it is the only thing that writes the audit line);
the **execution** is what the job runs, from the identifiers in its key, once or again after a
stop. Nothing is deleted without that order, nothing that Shelfhold did not write, nothing that
would leave an irreplaceable original without an independent copy, nothing outside the root.
"""

import contextlib
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.detach import (
    DETACH_JOB_KIND,
    DetachRefusedError,
    detach_job_key,
    detach_order_of,
    full_detach_phrase,
)
from shelfhold.export.application.remove_destination import (
    REMOVAL_JOB_KIND,
    RemovalMode,
    RemovalRefusedError,
    removal_job_key,
    removal_order_of,
    removal_phrase,
)
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.infrastructure.schema import replica as replica_schema
from shelfhold.library.public import independent_copies, irreplaceability
from tests.detach_env import OTHERS, DetachEnv, detach_env, fresh_admin
from tests.rename_env import export_chapters

pytestmark = pytest.mark.req("EF-EXP-031")

DELETE = RemovalMode.DELETE_FILES


class Killed(BaseException):
    """What a `kill -9` looks like from inside the process: nothing after it runs."""


@pytest.fixture
def d() -> Iterator[DetachEnv]:
    with detach_env() as made:
        export_chapters(made.env, "Dr Stone", ["1", "2", "3"])
        yield made


def _order(d: DetachEnv, mode: RemovalMode = DELETE) -> Any:
    dest = d.env.destination_id
    return d.remover.order(
        dest, mode, principal=fresh_admin(d.env), typed=removal_phrase(dest, mode)
    )


def _audit(d: DetachEnv, action: str) -> list[Any]:
    with d.env.engine.connect() as conn:
        return list(
            conn.execute(sa.text("SELECT target FROM audit WHERE action = :a"), {"a": action})
        )


def _units(d: DetachEnv) -> list[str]:
    """The Units behind the exported files, in the order of their paths (the fixture has a Unit
    of its own that no destination exports)."""
    with d.env.engine.connect() as conn:
        rows = conn.execute(
            sa.select(export.unit_coverage.c.unit_id)
            .select_from(
                export.unit_coverage.join(
                    export.export_record,
                    export.export_record.c.export_item_id == export.unit_coverage.c.carrier_id,
                )
            )
            .where(export.unit_coverage.c.carrier_type == "export_item")
            .order_by(export.export_record.c.rel_path)
        )
        return [str(u) for u in rows.scalars()]


def _make_irreplaceable(d: DetachEnv, unit: str, *, copies: int) -> None:
    """The source no longer lists the Unit, and `copies` independent copies are kept (0 or 1):
    the received archive of the library, which the destination's own row never replaces."""
    with d.env.engine.begin() as conn:
        conn.execute(
            sa.update(catalog.unit_variant)
            .where(catalog.unit_variant.c.unit_id == unit)
            .values(removed_at=d.env.clock.now())
        )
        if copies == 0:
            conn.execute(
                sa.delete(replica_schema.replica).where(
                    replica_schema.replica.c.unit_id == unit,
                    replica_schema.replica.c.location != "destination",
                )
            )
    with d.env.engine.connect() as conn:
        verdict = irreplaceability(conn, unit)
        assert verdict.irreplaceable and verdict.independent_copies == copies, verdict


# -- the order --------------------------------------------------------------------------------


def test_the_order_is_the_reserved_action_the_phrase_and_one_audit_line(d: DetachEnv) -> None:
    dest = d.env.destination_id
    before = (d.tree_digest(), d.intent_ids(), d.count(export.shelf_destination))
    phrase = removal_phrase(dest, DELETE)
    for who in OTHERS:  # a key, a stale session, a reader, a child
        with pytest.raises(RemovalRefusedError) as refused:
            d.remover.order(dest, DELETE, principal=who, typed=phrase)
        assert refused.value.reason == "not_allowed"
    for typed in (
        None,
        "",
        phrase + " ",
        phrase.upper(),
        removal_phrase(dest, RemovalMode.KEEP_FILES),
    ):
        with pytest.raises(RemovalRefusedError) as refused:
            d.remover.order(dest, DELETE, principal=fresh_admin(d.env), typed=typed)
        assert refused.value.reason == "confirmation_required"
    ghost = "01JNOSUCHDEST0000000000000"
    with pytest.raises(RemovalRefusedError) as unknown:  # the phrase is read first
        d.remover.order(ghost, DELETE, principal=fresh_admin(d.env), typed="x")
    assert unknown.value.reason == "confirmation_required"
    with pytest.raises(RemovalRefusedError) as unknown:  # right phrase, no such destination
        d.remover.order(
            ghost, DELETE, principal=fresh_admin(d.env), typed=removal_phrase(ghost, DELETE)
        )
    assert unknown.value.reason == "unknown_destination"
    assert (d.tree_digest(), d.intent_ids(), d.count(export.shelf_destination)) == before
    assert _audit(d, "destination.remove_all") == []  # refusals leave no line
    order = _order(d)
    assert (order.destination_id, order.mode) == (dest, DELETE)
    assert _audit(d, "destination.remove_all") == [(f"{dest}:delete_files",)]
    assert d.tree_digest() == before[0]  # an order deletes nothing: the job does


def test_the_job_key_carries_identifiers_and_no_phrase_no_password(d: DetachEnv) -> None:
    order = _order(d)
    key = removal_job_key(order)
    assert key.startswith(f"{REMOVAL_JOB_KIND}:") and order.destination_id in key
    assert removal_phrase(order.destination_id, DELETE) not in key
    assert removal_order_of(key) == order
    for forged in ("", "export.run:x", f"{REMOVAL_JOB_KIND}:", f"{REMOVAL_JOB_KIND}:a:everything"):
        with pytest.raises(ValueError, match="job key"):
            removal_order_of(forged)
    target = d.env.target_id
    detach = d.detacher.order(
        target, principal=fresh_admin(d.env), typed=full_detach_phrase(target),
        key_gone=True, credential_given=True, discard_hidden_data=True,
    )  # fmt: skip
    key = detach_job_key(detach)
    assert key.startswith(f"{DETACH_JOB_KIND}:") and target in key
    assert full_detach_phrase(target) not in key and "password" not in key
    assert detach_order_of(key) == detach
    with pytest.raises(ValueError, match="job key"):
        detach_order_of(f"{DETACH_JOB_KIND}:{target}:zz")


def test_a_detach_order_without_the_password_for_key_gone_is_refused(d: DetachEnv) -> None:
    target = d.env.target_id
    with pytest.raises(DetachRefusedError) as refused:
        d.detacher.order(
            target, principal=fresh_admin(d.env), typed=full_detach_phrase(target),
            key_gone=True, credential_given=False,
        )  # fmt: skip
    assert refused.value.reason == "credential_required"
    for who in OTHERS:
        with pytest.raises(DetachRefusedError) as closed:
            d.detacher.order(
                target, principal=who, typed=full_detach_phrase(target),
                key_gone=False, credential_given=True,
            )  # fmt: skip
        assert closed.value.reason == "not_allowed"
    assert _audit(d, "detach.full") == []


# -- what is deleted ----------------------------------------------------------------------------


def test_execute_takes_only_what_shelfhold_wrote_and_never_a_file_put_there_by_hand(
    d: DetachEnv,
) -> None:
    order = _order(d)
    first = d.env.files()[0]
    by_hand = first.parent / "cover-put-by-hand.jpg"
    by_hand.write_bytes(b"mine, not Shelfhold's")
    same_name = d.env.root / "Another series" / "Chapter 1.cbz"
    same_name.parent.mkdir()
    same_name.write_bytes(b"a file of the admin's, no line in the manifest")
    report = d.remover.execute(order)
    assert report.deleted == 3 and report.complete, report.left
    assert by_hand.read_bytes() == b"mine, not Shelfhold's"
    assert same_name.exists() and first.parent.is_dir()  # the folder with a hand-made file stays
    assert d.count(export.shelf_destination) == 0 and d.intents("pending", "applying") == []
    with d.env.engine.connect() as conn:  # nothing is left that names the destination
        assert (
            conn.execute(
                sa.select(sa.func.count())
                .select_from(replica_schema.replica)
                .where(replica_schema.replica.c.location == "destination")
            ).scalar_one()
            == 0
        )


def test_a_file_changed_since_the_export_is_left_and_said(d: DetachEnv) -> None:
    order = _order(d)
    victim = d.env.files()[0]
    victim.write_bytes(b"edited by its owner after the export")
    report = d.remover.execute(order)
    assert report.deleted == 2 and not report.complete
    assert [line.code for line in report.left] == ["changed_since_export"]
    assert victim.read_bytes() == b"edited by its owner after the export"
    assert d.count(export.shelf_destination) == 1  # not forgotten while something is left


# -- the last copy -----------------------------------------------------------------------------


def test_never_deletes_a_file_whose_unit_would_have_no_independent_copy_left(d: DetachEnv) -> None:
    """An irreplaceable original with no independent copy: the file of the destination may be the
    last bytes of it. It stays, said `sole_copy`; the other files go."""
    first = _units(d)[0]
    _make_irreplaceable(d, first, copies=0)
    report = d.remover.execute(_order(d))
    assert report.deleted == 2 and not report.complete
    assert [line.code for line in report.left] == ["sole_copy"]
    assert len(d.env.files()) == 1  # the one file of that Unit
    with d.env.engine.connect() as conn:
        assert independent_copies(conn, first) == 0  # the removal never made it worse
    assert d.count(export.shelf_destination) == 1 and d.intents("pending", "applying") == []


def test_deletes_when_an_independent_copy_is_kept_or_the_unit_can_be_fetched_again(
    d: DetachEnv,
) -> None:
    first, second = _units(d)[:2]
    _make_irreplaceable(d, first, copies=1)  # irreplaceable, but one copy is kept: it may go
    with d.env.engine.begin() as conn:  # `second`: replaceable, no copy at all: it may go
        conn.execute(
            sa.delete(replica_schema.replica).where(
                replica_schema.replica.c.unit_id == second,
                replica_schema.replica.c.location != "destination",
            )
        )
    with d.env.engine.connect() as conn:
        verdict = irreplaceability(conn, second)
        assert not verdict.irreplaceable and verdict.independent_copies == 0
    report = d.remover.execute(_order(d))
    assert report.deleted == 3 and report.complete, report.left
    assert d.env.files() == []


def test_the_last_copy_is_checked_again_when_an_interrupted_removal_resumes(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The intention was written when the Unit still had a copy; by the time the job resumes the
    copy is gone. The guard is read again, at the unlink, not only at the plan."""
    real = d.journal.mark_applying
    calls = {"n": 0}

    def die_once(*a: Any, **k: Any) -> Any:
        real(*a, **k)
        calls["n"] += 1
        if calls["n"] == 1:
            raise Killed

    monkeypatch.setattr(d.journal, "mark_applying", die_once)
    order = _order(d)
    with pytest.raises(Killed):
        d.remover.execute(order)
    monkeypatch.undo()
    for unit in _units(d):  # the copy that the first pass had counted on is lost meanwhile
        _make_irreplaceable(d, unit, copies=0)
    report = d.remover.execute(order)
    assert {line.code for line in report.left} == {"sole_copy"}
    assert len(d.env.files()) >= 1 and d.intents("pending", "applying") == []


# -- the attacks on the path --------------------------------------------------------------------


def test_attack_a_symlinked_folder_a_dotdot_and_an_absolute_path_never_leave_the_root(
    d: DetachEnv,
) -> None:
    outside = d.env.lib.library / "outside"
    outside.mkdir()
    bait = outside / "Chapter 1.cbz"
    bait.write_bytes(b"not Shelfhold's, but it has the name of a managed file")
    twin = outside / "twin.cbz"
    twin.write_bytes(b"another file outside the root")
    files = d.env.files()
    # a managed folder replaced by a link to a folder outside the root
    series_dir = files[0].parent
    names = sorted(p.name for p in series_dir.iterdir())
    kept = d.env.lib.library / "series-moved"
    series_dir.rename(kept)
    series_dir.symlink_to(outside, target_is_directory=True)
    for name in names:
        (outside / name).write_bytes((kept / name).read_bytes())  # same bytes, same hash
    with d.env.engine.begin() as conn:
        conn.execute(sa.text("PRAGMA ignore_check_constraints = ON"))
        records = list(conn.execute(sa.select(export.export_record)))
        extra = [r for r in records if not str(r.rel_path).startswith(series_dir.name)]
        for record, forged in zip(extra, ("../outside/twin.cbz", str(twin)), strict=False):
            conn.execute(
                sa.update(export.export_record)
                .where(export.export_record.c.export_item_id == record.export_item_id)
                .values(rel_path=forged)
            )
    d.remover.execute(_order(d))
    assert {p.name for p in outside.iterdir()} >= {"Chapter 1.cbz", "twin.cbz", *names}
    assert bait.read_bytes().startswith(b"not Shelfhold's")
    assert twin.read_bytes() == b"another file outside the root"
    assert d.intents("pending", "applying") == []


def test_execute_refuses_a_destination_that_is_gone_without_touching_anything(d: DetachEnv) -> None:
    order = _order(d)
    report = d.remover.execute(order)
    assert report.complete
    again = d.remover.execute(order)  # a replay of the job after it ended: nothing left to do
    assert (again.deleted, again.forgotten, again.complete) == (0, 0, True)


# -- kill -9 at each step ------------------------------------------------------------------------


@pytest.mark.parametrize("kill_at", range(18))
def test_a_kill_at_each_step_of_the_removal_is_finished_by_the_rerun(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch, kill_at: int
) -> None:
    """A stop after any of the steps (the intention, `applying`, the unlink, the close) leaves
    the next run to finish: no file deleted twice, no line without its file, no intention open,
    no copy row left for a file that is gone."""
    unlinked: list[str] = []
    ticks = {"n": 0}

    def after(owner: Any, name: str) -> None:
        real = getattr(owner, name)

        def wrapper(*a: Any, **k: Any) -> Any:
            result = real(*a, **k)
            if name == "purge_unlink":
                unlinked.append(str(a[1] if isinstance(owner, type) else a[0]))
            if ticks["n"] == kill_at:
                ticks["n"] += 1
                raise Killed
            ticks["n"] += 1
            return result

        monkeypatch.setattr(owner, name, wrapper)

    after(d.journal, "record")
    after(d.journal, "mark_applying")
    after(Fsops, "purge_unlink")
    after(d.journal, "mark_done")
    order = _order(d)
    with contextlib.suppress(Killed):  # the stop; past the last step it just runs to the end
        d.remover.execute(order)
    monkeypatch.undo()
    d.journal.resolve_open()  # the boot settles what a stop left open, before the queue restarts
    report = d.remover.execute(order)
    assert report.complete, report.left
    assert d.env.files() == []
    assert d.count(export.shelf_destination) == 0 and d.count(export.export_record) == 0
    assert d.intents("pending", "applying") == []
    assert len(unlinked) == len(set(unlinked)), "a file was deleted twice"
    with d.env.engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(sa.func.count())
                .select_from(replica_schema.replica)
                .where(replica_schema.replica.c.location == "destination")
            ).scalar_one()
            == 0
        )


# -- the full detachment is a resumable job -------------------------------------------------------


@pytest.mark.parametrize("kill_at", range(14))
def test_detach_is_a_resumable_job(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch, kill_at: int
) -> None:
    """Whatever step the process died after (an intention, the erasure, the settings, the key, the
    forgetting), the same order run again ends the detachment: the key is gone, the target is
    forgotten, the tree untouched, no intention open."""
    import shutil

    for name in ("recycle", "received"):  # nothing a detachment would refuse to destroy
        shutil.rmtree(d.root / ".shelfhold" / name, ignore_errors=True)
    shutil.rmtree(d.env.lib.manga / ".shelfhold", ignore_errors=True)
    exported = d.tree_digest()
    target = d.env.target_id
    ticks = {"n": 0}

    def after(owner: Any, name: str) -> None:
        real = getattr(owner, name)

        def wrapper(*a: Any, **k: Any) -> Any:
            result = real(*a, **k)
            if ticks["n"] == kill_at:
                ticks["n"] += 1
                raise Killed
            ticks["n"] += 1
            return result

        monkeypatch.setattr(owner, name, wrapper)

    after(d.journal, "record")
    after(d.journal, "mark_applying")
    after(Fsops, "erase_host_area")
    after(d.journal, "mark_done")
    after(d.revoker, "revoke_id")
    order = d.detacher.order(
        target, principal=fresh_admin(d.env), typed=full_detach_phrase(target),
        key_gone=False, credential_given=True,
    )  # fmt: skip
    run = {
        "client": d.wired.client,
        "revoker": d.revoker,
        "credential": "the account password",
    }
    report = None
    with contextlib.suppress(Killed):  # past the last step it runs to the end
        report = d.detacher.execute(order, **run)
    monkeypatch.undo()
    d.journal.resolve_open()  # the boot settles what a stop left open, before the queue restarts
    report = report or d.detacher.execute(order, **run)
    assert report.complete and report.key_revoked, report.leftovers
    assert d.tree_digest() == exported  # the exported files are never touched
    assert d.count(export.export_target) == 0 and d.count(export.shelf_destination) == 0
    assert d.intents("pending", "applying") == []
    assert d.wired.fake.auth_keys == [] or all(
        str(k["id"]) not in d.revoker.ids for k in d.wired.fake.auth_keys
    )
    again = d.detacher.execute(order, **run)  # the job replayed after its end changes nothing
    assert again.target_id == target and d.tree_digest() == exported


def test_the_detach_order_is_not_a_second_audit_when_the_job_ends(d: DetachEnv) -> None:
    import shutil

    for name in ("recycle", "received"):
        shutil.rmtree(d.root / ".shelfhold" / name, ignore_errors=True)
    shutil.rmtree(d.env.lib.manga / ".shelfhold", ignore_errors=True)
    target = d.env.target_id
    order = d.detacher.order(
        target, principal=fresh_admin(d.env), typed=full_detach_phrase(target),
        key_gone=False, credential_given=True,
    )  # fmt: skip
    d.detacher.execute(
        order, client=d.wired.client, revoker=d.revoker, credential="the account password"
    )
    assert _audit(d, "detach.full") == [(target,)]  # the order wrote it; the end writes none
