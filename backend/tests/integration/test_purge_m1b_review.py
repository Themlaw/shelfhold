"""Regressions of the adversarial review of L17 (EF-BIB-052, EF-BIB-053, EF-SRC-033).

Each test goes through the real path (the real `PurgeService`, the real `ConnectedUpstream`, the
real `FollowRequests`); the service is a fake only at the network edge."""

import re
from pathlib import Path

import pytest
import sqlalchemy as sa

from tests.export_env import export_env
from tests.purge_env import ADMIN, TITLE, spread
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
