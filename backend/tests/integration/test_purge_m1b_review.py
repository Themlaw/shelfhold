"""Regressions of the adversarial review of L17 (EF-BIB-052, EF-BIB-053, EF-SRC-033).

Each test goes through the real path (the real `PurgeService`, the real `ConnectedUpstream`, the
real `FollowRequests`); the service is a fake only at the network edge."""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import FollowRequests
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.purge import PurgeService
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition
from shelfhold.purge_upstream import ConnectedUpstream
from shelfhold.safeguard_jobs import service_refusal
from shelfhold.sdk.refs import SeriesRef
from shelfhold.sdk.source import ContentRemoved
from tests.export_env import ExportEnv, export_env
from tests.purge_env import ADMIN, TITLE, PurgeEnv, make_purge_env, spread
from tests.purge_m1b_env import FakeService, cite, fetched_ticket, make_m1b_env, write_index

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
