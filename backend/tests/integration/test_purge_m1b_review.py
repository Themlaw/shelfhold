"""Regressions of the adversarial review of L17 (EF-BIB-052, EF-BIB-053, EF-SRC-033).

Each test goes through the real path (the real `PurgeService`, the real `ConnectedUpstream`, the
real `FollowRequests`); the service is a fake only at the network edge."""

import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import FollowRequests
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.purge import (
    ALERT_DELETE_UPSTREAM,
    PurgeService,
    PurgeUnknownError,
)
from shelfhold.export.domain.physical_roots import RECEIVED_DIR
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition
from shelfhold.library.adapters.storage import KeepCopier, complete_keep, keep_rel
from shelfhold.library.public import PassthroughStore, PinnedCopy, StoreIndex, UnitGoneError
from shelfhold.purge_upstream import ConnectedUpstream
from shelfhold.safeguard_jobs import service_refusal
from shelfhold.sdk.refs import SeriesRef
from shelfhold.sdk.source import ContentRemoved
from shelfhold.work_locks import holder_over, index_holder_over
from tests.export_env import ExportEnv, export_env
from tests.integration.test_schema_catalog import add_unit, add_variant
from tests.library_env import cbz_bytes
from tests.purge_env import ADMIN, TITLE, PurgeEnv, make_purge_env, spread
from tests.purge_m1b_env import (
    FakeService,
    cite,
    fetched_ticket,
    index_is_coherent,
    index_units,
    make_m1b_env,
    write_index,
)

pytestmark = pytest.mark.req("EF-BIB-052", "EF-BIB-053", "EF-SRC-033")


def _database_bytes(engine: sa.Engine) -> bytes:
    database = Path(str(engine.url.database))
    found = b""
    for suffix in ("", "-wal"):
        path = Path(str(database) + suffix)
        if path.exists():
            found += path.read_bytes()
    return found


def test_purge_leaves_no_title_in_free_pages_or_the_wal(tmp_path: Path) -> None:
    """P1: a deleted row stays readable in a free page of the file, or in the log, until the page
    is reused. `secure_delete` blanks the page, the closing checkpoint empties the log."""
    with export_env() as env:
        service = FakeService(tmp_path)
        series = env.new_series(TITLE)
        unit, _ = spread(env, series)
        request = fetched_ticket(env, service, unit, "tok-1")
        purge = make_m1b_env(env, service)
        write_index(purge)
        cite(purge, request_id=request, unit=unit, title=TITLE)
        assert TITLE.encode() in _database_bytes(env.engine)  # the title was really stored
        report = purge.purge.purge_work(
            series.work_id, principal=ADMIN, typed=series.work_id, now=purge.clock.now()
        )
        assert report.complete
        raw = _database_bytes(env.engine)
        for needle in (TITLE, TITLE.lower(), TITLE.upper()):
            found = re.search(re.escape(needle.encode()), raw)
            around = raw[max(0, found.start() - 60) : found.end() + 40] if found else b""
            assert found is None, (needle, re.sub(rb"[^ -~]", b".", around))


# -- the series, through the real FollowRequests --------------------------------------------------

SLUG = "/manga/dr-stone"
PNG = b"\x89PNG\r\n\x1a\n2" + b"\x00" * 16


class _Service(FakeService):
    """The connected service, with the calls `FollowRequests.unfollow` really makes."""

    series_known = False

    async def instance_fingerprint(self, f: Any) -> str:
        return "fp-1"

    async def set_follow(
        self, f: Any, ref: Any, on: bool, *, remove_upstream: bool = False
    ) -> None:
        if not self.series_known:
            raise ContentRemoved(f"{ref.remote_id} is no longer known to the server")
        self.unfollowed.append(("suwayomi", ref.source_key, ref.remote_id))


def _followed_work(env: ExportEnv, tmp_path: Path) -> tuple[_Service, Any, str, str, PurgeEnv]:
    """A Work bound to a series of the service, with its `follow_request`, and a purge whose
    adapter is wired as `connected_upstream` wires it (the real `FollowRequests.unfollow`)."""
    service = _Service(tmp_path)
    series = env.new_series(TITLE)
    unit, _ = spread(env, series)
    items = acquisition.source_item
    with env.engine.begin() as connection:
        item = connection.execute(
            sa.select(acquisition.source_binding.c.source_item_id).where(
                acquisition.source_binding.c.work_id == series.work_id
            )
        ).scalar_one()
        source_key = connection.execute(
            sa.select(items.c.source_key).where(items.c.id == item)
        ).scalar_one()
        connection.execute(
            sa.update(items).where(items.c.id == item).values(plugin_id="suwayomi", remote_id=SLUG)
        )
        connection.execute(
            sa.insert(acquisition.follow_request).values(
                id=new_ulid(), source_key=source_key, remote_id=SLUG, state="followed"
            )
        )
    base = make_purge_env(env.lib)
    link = SimpleNamespace(service=service, fetcher=object())
    requests = FollowRequests(env.engine, base.clock, lambda: link)

    async def unfollow(plugin_id: str, source_key: str, remote_id: str) -> None:
        ref = SeriesRef(source_key=source_key, remote_id=remote_id, title="")
        await requests.unfollow(ref, remove_upstream=True, confirmed=True)

    upstream = ConnectedUpstream(
        env.engine, env.lib.fsops, IntentJournal(env.engine, env.lib.fsops, clock=base.clock),
        service_refusal, service, object(), base.clock, unfollow=unfollow,
    )  # fmt: skip
    base.purge = PurgeService(
        env.engine, env.lib.fsops, base.tombstones, clock=base.clock, data_dir=env.lib.data,
        bus=base.bus, fresh_auth_s=900, store_dir=env.lib.store, upstream=upstream,
    )  # fmt: skip
    return service, series, unit, source_key, base


def test_purge_work_completes_when_the_series_is_already_gone_from_the_service(
    tmp_path: Path,
) -> None:
    """P2: the admin removed the series in Suwayomi, as the M1a alert told. The service answers
    `source.content_removed`: that series is out of the library, which is what the purge wants."""
    with export_env() as env:
        _service, series, _unit, _key, purge = _followed_work(env, tmp_path)
        report = purge.purge.purge_work(
            series.work_id, principal=ADMIN, typed=series.work_id, now=purge.clock.now()
        )
        assert report.complete, report.refused
        assert report.unfollowed == 1
        with env.engine.connect() as connection:
            assert not connection.execute(
                sa.text("SELECT id FROM work WHERE id = :w"), {"w": series.work_id}
            ).all()


def test_purge_work_erases_the_follow_request_and_the_series_url(tmp_path: Path) -> None:
    """P3: `follow_request` is keyed by `(source_key, remote_id)`, not by a foreign key: the row
    kept the URL slug of the series (a title) after the purge."""
    with export_env() as env:
        service, series, _unit, key, purge = _followed_work(env, tmp_path)
        service.series_known = True
        report = purge.purge.purge_work(
            series.work_id, principal=ADMIN, typed=series.work_id, now=purge.clock.now()
        )
        assert report.complete and service.unfollowed == [("suwayomi", key, SLUG)]
        with env.engine.connect() as connection:
            rows = connection.execute(sa.select(acquisition.follow_request.c.remote_id)).all()
            assert rows == []
            for name in connection.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type = 'table'")
            ).scalars():
                found = connection.execute(sa.text(f'SELECT * FROM "{name}"'))  # noqa: S608
                blob = json.dumps([[str(c) for c in r] for r in found])
                assert "dr-stone" not in blob, name


# -- a deed that was begun is never sent again, by any door ---------------------------------------


def test_ui_login_deed_killed_after_the_release_is_never_sent_twice(tmp_path: Path) -> None:
    """P4: another account keeps the file, so the chapter is still there after our `release`.
    Killed right after it: the intent stays `applying` and nothing tells whether it landed. The
    purge settles it as `kept_by_other_account` (alert: delete in Suwayomi); no later run, and not
    `intents resolve --abandon`, sends a second DELETE."""
    from tests.integration.test_purge_m1b_kill import killed_run, restart

    with export_env() as env:
        service = FakeService(tmp_path, shared={"ticket-1"})
        series = env.new_series(TITLE)
        unit, _ = spread(env, series)
        fetched_ticket(env, service, unit, "ticket-1")
        purge = make_m1b_env(env, service)
        write_index(purge)
        killed_run(purge, unit, "release_sent")
        assert service.released() == ["ticket-1"]
        again = restart(env, service)
        report = again.purge.purge_unit(unit, principal=ADMIN, typed=unit, now=again.clock.now())
        assert report.complete and report.upstream_kept_by_others == 1
        assert ALERT_DELETE_UPSTREAM in report.alerts
        journal = IntentJournal(env.engine, env.lib.fsops, clock=again.clock)
        # the intent is closed with the claim: nothing is left for `intents resolve --abandon`
        assert journal.open_payloads("purge_upstream") == []
        with pytest.raises(PurgeUnknownError):  # the Unit is gone: nothing runs a second time
            again.purge.purge_unit(unit, principal=ADMIN, typed=unit, now=again.clock.now())
        assert service.released() == ["ticket-1"]  # one DELETE, ever


def test_abandoning_a_begun_deed_withdraws_the_ticket(tmp_path: Path) -> None:
    """P4: the admin's door (`intents resolve --abandon`) on an `applying` erasure closes the
    request's ticket in the same transaction, so that nothing asks the service again."""
    with export_env() as env:
        service = FakeService(tmp_path, shared={"ticket-1"})
        unit, _ = spread(env, env.new_series(TITLE))
        request = fetched_ticket(env, service, unit, "ticket-1")
        purge = make_m1b_env(env, service)
        journal = IntentJournal(env.engine, env.lib.fsops, clock=purge.clock)
        begun = journal.record(
            op="purge_upstream", expected_file_id=PinnedCopy(0, 0, 0, request_id=request)
        )
        journal.mark_applying(begun)
        assert journal.abandon(begun)
        with env.engine.connect() as connection:
            ticket = connection.execute(
                sa.select(acquisition.download_request.c.ticket).where(
                    acquisition.download_request.c.id == request
                )
            ).scalar_one()
        assert ticket is None


# -- a window write and a purge never interleave --------------------------------------------------


def _store_files(env: ExportEnv) -> list[Path]:
    return [p for p in env.lib.store.rglob("*.cbz") if p.is_file()]


def test_keep_and_purge_never_leave_a_readable_orphan_in_store() -> None:
    """P5 (keep): the keep ("Mettre à l'abri") copies the file while `purge --now` runs on the same
    Work. The two share the lock of the Work: the purge is refused while the copy is made, and the
    run that follows erases the copy that the registry now knows."""
    with export_env() as env:
        series = env.new_series(TITLE)
        unit = env.add_chapter(series, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        purge = make_m1b_env(env)
        journal = IntentJournal(env.engine, env.lib.fsops, clock=purge.clock)
        keeper = KeepCopier(
            env.engine, env.lib.fsops, env.lib.locate, str(env.lib.store), env.lib.data, journal,
            hold_work=holder_over(env.engine, purge.clock),
        )  # fmt: skip
        during: list[object] = []
        real = Fsops.copy_independent

        def copy_then_purge(self: Fsops, *args: Any, **kwargs: Any) -> Any:
            done = real(self, *args, **kwargs)  # the copy has landed in /store

            def attempt() -> None:
                try:
                    during.append(
                        purge.purge.purge_unit(
                            unit, principal=ADMIN, typed=unit, now=purge.clock.now()
                        )
                    )
                except Exception as error:
                    during.append(error)

            thread = threading.Thread(target=attempt)
            thread.start()
            thread.join()
            return done

        Fsops.copy_independent = copy_then_purge  # type: ignore[method-assign]
        try:
            keeper.keep(unit)
        finally:
            Fsops.copy_independent = real  # type: ignore[method-assign]
        assert len(during) == 1 and isinstance(during[0], Exception)  # refused: the Work is held
        report = purge.purge.purge_unit(unit, principal=ADMIN, typed=unit, now=purge.clock.now())
        assert report.complete
        assert _store_files(env) == []


def test_keep_of_a_work_a_purge_holds_is_work_busy() -> None:
    from shelfhold.export.application.locks import ResourceLocks, work_resource
    from shelfhold.library.domain.failures import SafeguardFailedError

    with export_env() as env:
        series = env.new_series(TITLE)
        unit = env.add_chapter(series, "1")
        purge = make_m1b_env(env)
        keeper = KeepCopier(
            env.engine, env.lib.fsops, env.lib.locate, str(env.lib.store), env.lib.data,
            IntentJournal(env.engine, env.lib.fsops, clock=purge.clock),
            hold_work=holder_over(env.engine, purge.clock, wait=0.0),
        )  # fmt: skip
        locks = ResourceLocks(env.engine, clock=purge.clock)
        with (
            locks.hold_resources(new_ulid(), [work_resource(series.work_id)]),
            pytest.raises(SafeguardFailedError) as refused,
        ):
            keeper.keep(unit)
        assert refused.value.reason == "work_busy" and _store_files(env) == []


def test_replayed_keep_of_a_purged_content_removes_the_copy() -> None:
    """P5: a `copy_cross_root` intent that was left by a kill, its content row gone (the purge ran
    first): the boot's bookkeeping used to close it and leave the copy. It takes the copy away."""
    with export_env() as env:
        series = env.new_series(TITLE)
        unit = env.add_chapter(series, "1")
        with env.engine.connect() as connection:
            content_id, size = connection.execute(
                sa.text("SELECT id, size FROM content WHERE unit_id = :u"), {"u": unit}
            ).one()
        target = env.lib.store / keep_rel(unit, content_id)
        env.lib.fsops.write_atomic(str(target), b"PK-readable-copy", parents=True)
        with env.engine.begin() as connection:
            connection.execute(sa.text("DELETE FROM content WHERE id = :c"), {"c": content_id})
            complete_keep(
                connection,
                env.lib.fsops,
                env.lib.locate,
                content_id,
                str(target),
                None,
                datetime.now(UTC),
            )
        assert not target.exists() and size


def test_ingest_of_a_chapter_for_a_purged_unit_writes_nothing() -> None:
    """P5 (ingest): the follow cycle files a chapter while `purge --work` runs. Under the lock the
    Unit is looked at again: it is gone, nothing is written and nothing readable is left."""
    with export_env() as env:
        series = env.new_series(TITLE)
        spread(env, series)
        purge = make_m1b_env(env)
        with env.engine.begin() as connection:
            second = add_unit(connection, series.edition_id, "2")
            variant = add_variant(connection, second, series.binding_id, "ch-two")
        store = PassthroughStore(
            env.engine, env.lib.fsops, env.lib.locate, env.lib.data, tombstones=purge.tombstones,
            hold_work=holder_over(env.engine, purge.clock),
        )  # fmt: skip
        assert purge.purge.purge_work(
            series.work_id, principal=ADMIN, typed=series.work_id, now=purge.clock.now()
        ).complete
        dest = env.lib.manga / RECEIVED_DIR / f"{second}.cbz"
        staged = env.lib.stage(cbz_bytes((("2-001.png", PNG),)))
        with pytest.raises(UnitGoneError):
            store.ingest(unit_id=second, variant_id=variant, staged=staged, dest=str(dest))
        assert not dest.exists()


def test_ingest_failing_after_the_write_takes_the_file_away() -> None:
    """P5: the rows refuse the file after it was placed (the Unit went between the look and the
    insert): the file is removed, never left readable."""
    with export_env() as env:
        series = env.new_series(TITLE)
        spread(env, series)
        purge = make_m1b_env(env)
        with env.engine.begin() as connection:
            second = add_unit(connection, series.edition_id, "2")
            variant = add_variant(connection, second, series.binding_id, "ch-two")
        store = PassthroughStore(
            env.engine, env.lib.fsops, env.lib.locate, env.lib.data, tombstones=purge.tombstones
        )
        real = Fsops.write_atomic
        dest = env.lib.manga / RECEIVED_DIR / f"{second}.cbz"

        def write_then_drop(self: Fsops, local: str, *args: Any, **kwargs: Any) -> Any:
            done = real(self, local, *args, **kwargs)
            if local == str(dest):
                with env.engine.begin() as connection:
                    connection.execute(sa.text("PRAGMA foreign_keys=ON"))
                    connection.execute(
                        sa.text("DELETE FROM unit_variant WHERE id = :v"), {"v": variant}
                    )
            return done

        Fsops.write_atomic = write_then_drop  # type: ignore[method-assign]
        try:
            with pytest.raises(sa.exc.IntegrityError):
                store.ingest(
                    unit_id=second,
                    variant_id=variant,
                    staged=env.lib.stage(
                        cbz_bytes((("2-001.png", b"\x89PNG\r\n\x1a\n2" + b"\x00" * 16),))
                    ),
                    dest=str(dest),
                )
        finally:
            Fsops.write_atomic = real  # type: ignore[method-assign]
        assert not dest.exists()


# -- the index of /store, one writer at a time ----------------------------------------------------


def test_window_index_render_never_overwrites_a_purge_that_ran_meanwhile() -> None:
    """P6: the window's pass renders `INDEX.jsonl`, `purge --now` runs, the pass writes: the file
    cited the purged Unit again (and its title). Both take the lock of the index, so the purge
    waits for the pass (and is refused when it is held too long), and the run that follows leaves
    an index that is coherent and says nothing of the Unit."""
    from shelfhold.export.application.locks import LockBusyError

    with export_env() as env:
        series = env.new_series(TITLE)
        unit, _ = spread(env, series)
        other, _ = spread(env, series, "2")
        clock = make_purge_env(env.lib).clock
        purge = make_m1b_env(env, index_lock=index_holder_over(env.engine, clock, wait=0.2))
        write_index(purge)
        window = StoreIndex(
            env.engine, env.lib.fsops, str(env.lib.store), purge.tombstones,
            hold=index_holder_over(env.engine, clock),
        )  # fmt: skip
        during: list[object] = []
        real = StoreIndex.render

        def render(self: StoreIndex, **kwargs: Any) -> Any:
            out = real(self, **kwargs)
            if self is window:  # the purge starts while the pass holds the lock

                def attempt() -> None:
                    try:
                        during.append(
                            purge.purge.purge_unit(
                                unit, principal=ADMIN, typed=unit, now=purge.clock.now()
                            )
                        )
                    except Exception as error:
                        during.append(error)

                thread = threading.Thread(target=attempt)
                thread.start()
                thread.join()
            return out

        StoreIndex.render = render  # type: ignore[method-assign]
        try:
            window.rewrite()
        finally:
            StoreIndex.render = real  # type: ignore[method-assign]
        assert len(during) == 1 and isinstance(during[0], LockBusyError)
        assert index_is_coherent(purge)  # the purge went no further: its copies are still there
        report = purge.purge.purge_unit(unit, principal=ADMIN, typed=unit, now=purge.clock.now())
        assert report.complete
        assert unit not in index_units(purge) and other in index_units(purge)
        assert index_is_coherent(purge)
        assert unit not in (env.lib.store / "INDEX.jsonl").read_text()
