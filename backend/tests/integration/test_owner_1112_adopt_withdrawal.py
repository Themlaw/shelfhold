"""Owner decision 2026-11-12: the preview of the adoption of a child library offers a removal plan
for the unrated Works whose files the children can read. The plan is versioned like
`shelf withdraw`, the admin confirms it, and nothing moves without that (so "aucun fichier ne
bouge" still holds for the adoption itself)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.withdrawal import WithdrawalRefusedError, WithdrawalService
from shelfhold.infrastructure.follow_suspended import suspended_work_ids
from shelfhold.infrastructure.schema import catalog, export
from tests.adopt_env import adoption
from tests.family_env import NOW, admin, family_env
from tests.integration.test_child_adoption import _child_library, _rated_before
from tests.integration.test_family_shelves import _key
from tests.kavita_env import run

pytestmark = pytest.mark.req("EF-EXP-048", "EF-BIB-037")

UNRATED = {
    "Dr Stone": ["Dr Stone/Dr Stone - Ch.1.cbz", "Dr Stone/Dr Stone - Ch.2.cbz"],
    "Monster": ["Monster/Monster - Vol.1.cbz", "Monster/Monster - Vol.2.cbz"],
}


def _service(env, scans):  # type: ignore[no-untyped-def]
    return WithdrawalService(
        env.engine, env.lib.fsops, clock=env.clock, request_scan=scans.append,
        store_dir=str(env.lib.store), locate=env.lib.locate,
    )  # fmt: skip


def _preview(a, env):  # type: ignore[no-untyped-def]
    return run(a.adopter.preview(a.wired.client, env.destination_id))


def test_the_preview_offers_a_versioned_plan_and_writes_nothing() -> None:
    with family_env(13) as env:
        _rated_before(env)
        a = _child_library(env)
        before = a.state()
        plan = _preview(a, env).withdrawal_plan
        assert {s.name: list(s.files) for s in plan.series} == UNRATED
        assert plan == _preview(a, env).withdrawal_plan  # stable
        assert a.state() == before  # nothing moved, nothing recycled
        with env.engine.connect() as connection:
            assert suspended_work_ids(connection) == frozenset()


def test_no_plan_for_a_library_the_children_cannot_read() -> None:
    with family_env(18) as env:
        a = adoption(env)  # an adult shelf, no restricted account: not a child library
        a.add_series("Dr Stone", ["Dr Stone - Ch.1.cbz"])
        report = _preview(a, env)
        assert not report.child_library
        assert report.withdrawal_plan.series == ()


def test_adopting_moves_no_file_even_with_a_plan_offered() -> None:
    with family_env(13) as env:
        _rated_before(env)
        a = _child_library(env)
        before = a.state()
        a.adopt()
        assert a.state() == before  # the plan is only offered: nothing without confirmation
        assert _service(env, []).adopted_unrated_plan(env.destination_id).items


def test_the_plan_read_after_adoption_lists_the_files_of_the_preview() -> None:
    """The version binds the export items and the bytes, which exist only after the adoption: the
    preview shows the files, the plan read afterwards carries the version to confirm."""
    with family_env(13) as env:
        _rated_before(env)
        a = _child_library(env)
        seen = _preview(a, env).withdrawal_plan
        a.adopt()
        plan = _service(env, []).adopted_unrated_plan(env.destination_id)
        assert sorted(f.rel_path for i in plan.items for f in i.files) == sorted(
            f for series in seen.series for f in series.files
        )
        assert plan.version


def test_confirmed_plan_takes_the_unrated_files_out_and_only_them() -> None:
    with family_env(13) as env:
        _rated_before(env)
        a = _child_library(env)
        a.adopt()
        scans: list[str] = []
        service = _service(env, scans)
        report = service.apply(
            if_match=service.adopted_unrated_plan(env.destination_id).version,
            principal=admin(),
            now=NOW,
            destination_id=env.destination_id,
        )
        assert report.files == 4 and len(report.withdrawn) == 2
        left = _served(a, env)
        assert left == {"Akira - Ch.1.cbz", "Lupin - Ch.1.cbz"}  # the rated ones stay
        assert scans, "Kavita is asked to scan the folders the files left"
        with env.engine.connect() as connection:
            audited = connection.execute(
                sa.text("SELECT action FROM audit WHERE action = 'shelf.withdraw'")
            ).all()
            shelves = connection.execute(
                sa.select(catalog.edition.c.shelf_id).where(
                    catalog.edition.c.work_id.in_(suspended_work_ids(connection))
                )
            ).scalars()
            assert set(shelves) == {None}
            assert suspended_work_ids(connection)  # still waiting to be rated
        assert len(audited) == 2
        assert {r.state for r in env.rows(export.export_record)} >= {"recycled"}


def test_nothing_moves_without_the_exact_plan_or_an_admin() -> None:
    """Attack: a stale or empty version, a key, a lapsed session: refused, files stay."""
    with family_env(13) as env:
        _rated_before(env)
        a = _child_library(env)
        a.adopt()
        before = a.state()
        service = _service(env, [])
        where = {"destination_id": env.destination_id}
        version = service.adopted_unrated_plan(env.destination_id).version
        for bad in ("", "0" * 32, version[:-1] + "x"):
            with pytest.raises(StalePlanError):
                service.apply(if_match=bad, principal=admin(), now=NOW, **where)
        with pytest.raises(WithdrawalRefusedError):
            service.apply(if_match=version, principal=_key(), now=NOW, **where)
        assert a.state() == before


def test_a_rating_that_lands_in_between_makes_the_plan_stale() -> None:
    with family_env(13) as env:
        _rated_before(env)
        a = _child_library(env)
        a.adopt()
        stale = _service(env, []).adopted_unrated_plan(env.destination_id).version
        with env.engine.connect() as connection:
            work_id = connection.execute(
                sa.select(catalog.work.c.id).where(catalog.work.c.title == "Monster")
            ).scalar_one()
        from shelfhold.catalog.public import RatingService
        from shelfhold.sdk.age import AgeRating

        with env.engine.begin() as connection:
            RatingService().rate(connection, work_id, AgeRating(10), principal=admin(), now=NOW)
        before = a.state()
        with pytest.raises(StalePlanError):
            _service(env, []).apply(
                if_match=stale, principal=admin(), now=NOW,
                destination_id=env.destination_id,
            )  # fmt: skip
        assert a.state() == before  # the rated Work is not taken out on an old reading


def test_a_rated_work_is_never_in_the_plan() -> None:
    with family_env(13) as env:
        known = _rated_before(env)
        a = _child_library(env)
        a.adopt()
        plan = _service(env, []).adopted_unrated_plan(env.destination_id)
        names = {f.rel_path.split("/", 1)[0] for i in plan.items for f in i.files}
        assert names == {"Dr Stone", "Monster"}
        assert known  # Akira and Lupin were rated before the adoption


def test_the_cli_preview_prints_the_plan_and_the_command_to_confirm(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from shelfhold.interfaces.cli.adopt import _show

    with family_env(13) as env:
        _rated_before(env)
        a = _child_library(env)
        report = _preview(a, env)
        _show(report)
        out = capsys.readouterr().out
        assert "Dr Stone" in out and "Monster" in out
        assert f"shelf withdraw --adopted {env.destination_id}" in out
        assert "Akira" not in out.split("Plan de retrait", 1)[1]  # a rated Work is never planned


def test_the_withdraw_command_takes_the_adopted_destination() -> None:
    from shelfhold.interfaces.cli.registry import build_parser

    args = build_parser().parse_args(["shelf", "withdraw", "--adopted", "D1"])
    assert args.adopted == "D1"
    assert build_parser().parse_args(["shelf", "withdraw"]).adopted is None


def test_a_plan_for_an_unknown_destination_is_refused_not_empty() -> None:
    with family_env(13) as env, pytest.raises(LookupError):
        _service(env, []).adopted_unrated_plan("no-such-destination")


# -- review of the PR: no loss of an original, read-only roots, a version that binds the bytes ---


def _served(a, env):  # type: ignore[no-untyped-def]
    """The files left in the served library (not the bins, not /store)."""
    return {
        p.name
        for p in a.env.lib.tmp.rglob("*.cbz")
        if ".shelfhold" not in p.parts and env.lib.store not in p.parents
    }


def _adopted(env):  # type: ignore[no-untyped-def]
    _rated_before(env)
    a = _child_library(env)
    a.adopt()
    return a


def _all_bytes(root, names):  # type: ignore[no-untyped-def]
    return {p.name: p.read_bytes() for p in root.rglob("*.cbz") if p.name in names}


def test_an_adopted_original_is_kept_in_the_store_before_it_leaves_the_library() -> None:
    """The files adopted are the only copy: each gets a verified independent copy in /store."""
    with family_env(13) as env:
        a = _adopted(env)
        names = {
            "Dr Stone - Ch.1.cbz",
            "Dr Stone - Ch.2.cbz",
            "Monster - Vol.1.cbz",
            "Monster - Vol.2.cbz",
        }
        original = _all_bytes(a.env.lib.library, names) or _all_bytes(a.env.root, names)
        assert set(original) == names
        service = _service(env, [])
        service.apply(
            if_match=service.adopted_unrated_plan(env.destination_id).version,
            principal=admin(), now=NOW, destination_id=env.destination_id,
        )  # fmt: skip
        kept = [p for p in env.lib.store.rglob("*") if p.is_file() and ".shelfhold" not in p.parts]
        assert sorted(p.read_bytes() for p in kept if p.suffix == ".cbz") == sorted(
            original.values()
        )
        with env.engine.connect() as connection:
            rows = connection.execute(
                sa.text("SELECT independent, file_hash FROM replica WHERE location = 'store'")
            ).all()
        assert len(rows) == 4 and all(r[0] for r in rows)


def test_the_30_day_sweep_never_removes_an_adopted_original_without_a_verified_copy() -> None:
    """Attack (PoC of the review): bin entries older than 30 days with no independent copy stay."""
    import os
    import time

    from shelfhold.export.application.intents import IntentJournal
    from shelfhold.export.application.recycle_sweep import RecycleSweeper

    with family_env(13) as env:
        a = _adopted(env)
        service = _service(env, [])
        service.apply(
            if_match=service.adopted_unrated_plan(env.destination_id).version,
            principal=admin(), now=NOW, destination_id=env.destination_id,
        )  # fmt: skip
        with env.engine.begin() as connection:  # the copies are lost (or were never verified)
            connection.execute(sa.text("DELETE FROM replica WHERE location = 'store'"))
        recycle = next(p for p in a.env.lib.tmp.rglob("recycle") if p.is_dir())
        old = time.time() - 31 * 86400
        for entry in recycle.iterdir():
            os.utime(entry, (old, old))
        sweeper = RecycleSweeper(
            env.lib.fsops, IntentJournal(env.engine, env.lib.fsops, clock=env.clock),
            [str(recycle.parent.parent)],
        )  # fmt: skip
        report = sweeper.sweep()
        assert report.removed == 0
        kept = [p for p in a.env.lib.tmp.rglob("*.cbz") if "recycle" in p.parts]
        assert len(kept) == 4  # the originals are still there


def test_the_sweep_still_removes_what_has_a_verified_copy() -> None:
    import os
    import time

    from shelfhold.export.application.intents import IntentJournal
    from shelfhold.export.application.recycle_sweep import RecycleSweeper

    with family_env(13) as env:
        a = _adopted(env)
        service = _service(env, [])
        service.apply(
            if_match=service.adopted_unrated_plan(env.destination_id).version,
            principal=admin(), now=NOW, destination_id=env.destination_id,
        )  # fmt: skip
        recycle = next(p for p in a.env.lib.tmp.rglob("recycle") if p.is_dir())
        old = time.time() - 31 * 86400
        for entry in recycle.iterdir():
            os.utime(entry, (old, old))
        sweeper = RecycleSweeper(
            env.lib.fsops, IntentJournal(env.engine, env.lib.fsops, clock=env.clock),
            [str(recycle.parent.parent)],
        )  # fmt: skip
        assert sweeper.sweep().removed >= 1
        assert len([p for p in env.lib.store.rglob("*.cbz")]) == 4  # the copies stay


def test_a_read_only_root_refuses_the_plan_and_the_application_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shelfhold.export.application.withdrawal import WithdrawalRootReadOnlyError

    with family_env(13) as env:
        a = _adopted(env)
        before = a.state()
        monkeypatch.setattr(env.lib.fsops, "is_read_only", lambda _local: True)
        service = _service(env, [])
        with pytest.raises(WithdrawalRootReadOnlyError) as refused:
            service.adopted_unrated_plan(env.destination_id)
        assert "read-only" in str(refused.value)
        with pytest.raises(WithdrawalRootReadOnlyError):
            service.apply(
                if_match="anything", principal=admin(), now=NOW, destination_id=env.destination_id
            )
        assert a.state() == before
        assert {r.state for r in env.rows(export.export_record)} == {"adopted"}


def test_the_database_changes_only_after_every_move_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A move that fails leaves the records, the Editions and the plan as they were."""
    from shelfhold.infrastructure.fsops.errors import FsopsError

    with family_env(13) as env:
        a = _adopted(env)
        service = _service(env, [])
        version = service.adopted_unrated_plan(env.destination_id).version

        def failing(_local: str) -> str:
            raise FsopsError("the move failed")

        monkeypatch.setattr(env.lib.fsops, "recycle", failing)
        report = service.apply(
            if_match=version, principal=admin(), now=NOW, destination_id=env.destination_id
        )
        assert report.withdrawn == () and report.pending
        assert {r.state for r in env.rows(export.export_record)} == {"adopted"}
        assert service.adopted_unrated_plan(env.destination_id).items  # the plan still lists them
        with env.engine.connect() as connection:
            audited = connection.execute(
                sa.text("SELECT count(*) FROM audit WHERE action = 'shelf.withdraw'")
            ).scalar_one()
        assert audited == 0
        assert {"Dr Stone - Ch.1.cbz", "Monster - Vol.1.cbz"} <= _served(a, env)


def test_the_version_binds_the_bytes_of_the_files() -> None:
    """Attack: a file replaced after the plan was read (same path, other bytes) makes it stale."""
    with family_env(13) as env:
        a = _adopted(env)
        service = _service(env, [])
        version = service.adopted_unrated_plan(env.destination_id).version
        target = next(p for p in a.env.lib.tmp.rglob("Dr Stone - Ch.1.cbz"))
        target.write_bytes(target.read_bytes() + b"changed")
        report = service.apply(
            if_match=version, principal=admin(), now=NOW, destination_id=env.destination_id
        )
        assert target.exists() and target.read_bytes().endswith(b"changed")  # not the shown file
        assert str(target) in report.pending or any(target.name in p for p in report.pending)


def test_a_destination_that_is_not_a_child_library_has_no_plan() -> None:
    from shelfhold.export.application.withdrawal import NotChildLibraryError

    with family_env(13) as env:
        _adopted(env)
        with env.engine.begin() as connection:  # an adult shelf that nobody restricted
            connection.execute(
                sa.update(export.shelf).values(max_age_rating=18, follow_suspended=[])
            )
        service = _service(env, [])
        with pytest.raises(NotChildLibraryError):
            service.adopted_unrated_plan(env.destination_id)
        with pytest.raises(NotChildLibraryError):
            service.apply(
                if_match="x", principal=admin(), now=NOW, destination_id=env.destination_id
            )
