"""M4 L14a: `reading_progress` merges the positions of a reader (EF-LEC-014, EF-LEC-007, EF-BIB-026).

The view keeps, per Unit, the furthest state any position reached, `client_ts` bounded by
`server_ts`; a Unit that was read never goes back to unread; a volume page is projected on the
Units the file covers; a position taken on a replaced content is `approximate`. The pure rule of
`reading.domain` and the SQL view are proved equal on a grid.
"""

from datetime import timedelta
from pathlib import Path

import pytest

from shelfhold.reading.domain.projection import Bounds, project
from tests.reading_env import NEW_HASH, OLD_HASH, T0, hours, reading_env

pytestmark = pytest.mark.req("EF-LEC-014")


def states(env, user: str, units: list[str]) -> list[str]:  # type: ignore[no-untyped-def]
    got = env.progress(user)
    return [got[u].state if u in got else "-" for u in units]


def test_read_unit_never_goes_back_to_unread(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 3)
        volume = env.file("ok", units)
        finished = env.position(reader, volume, percent=1.0, client_ts=hours(0))
        assert states(env, reader, units) == ["read", "read", "read"]
        # reopened at the very start, later: a restart, a different device, a restore
        env.position(reader, volume, percent=0.0, client_ts=hours(5), server_ts=hours(5))
        env.position(reader, volume, percent=0.05, client_ts=hours(6), server_ts=hours(6))
        assert states(env, reader, units) == ["read", "read", "read"]
        assert {r.last_position_id for r in env.progress(reader).values()} == {finished}


def test_an_older_position_arriving_late_changes_nothing(tmp_path: Path) -> None:
    """A reader that was offline sends what it had before: by `client_ts` it is the oldest."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 4)
        volume = env.file("ok", units)
        env.position(reader, volume, percent=0.8, client_ts=hours(10), server_ts=hours(10))
        late = env.position(reader, volume, percent=0.3, client_ts=hours(1), server_ts=hours(11))
        got = env.progress(reader)
        assert states(env, reader, units) == ["read", "read", "read", "unread"]
        assert late not in {r.last_position_id for r in got.values()}


def test_a_position_further_on_moves_the_state_forward(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 4)
        volume = env.file("ok", units)
        env.position(reader, volume, percent=0.3, client_ts=hours(1))
        assert states(env, reader, units) == ["read", "in_progress", "unread", "unread"]
        env.position(reader, volume, percent=0.9, client_ts=hours(2))
        assert states(env, reader, units) == ["read", "read", "read", "in_progress"]


def test_client_ts_in_the_future_is_bounded_by_server_ts(tmp_path: Path) -> None:
    """A clock a year ahead does not make a position the latest: it ties at the server's date."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 1)
        item = env.file("ok", units)
        honest = env.position(reader, item, percent=0.5, client_ts=hours(5), server_ts=hours(5))
        env.position(
            reader, item, percent=0.5, client_ts=T0 + timedelta(days=365), server_ts=hours(2)
        )  # bounded to hour 2: older than the honest one
        assert env.progress(reader)[units[0]].last_position_id == honest


def test_furthest_position_is_the_one_kept(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 1)
        item = env.file("ok", units)
        far = env.position(reader, item, percent=0.9, client_ts=hours(1))
        env.position(reader, item, percent=0.2, client_ts=hours(9), server_ts=hours(9))
        assert env.progress(reader)[units[0]].last_position_id == far


@pytest.mark.req("EF-LEC-014", "EF-BIB-026")
def test_kavita_volume_progress_projects_to_covered_units(tmp_path: Path) -> None:
    """A volume of 8 chapters with known bounds, Kavita says page 143: chapters 1 to 5 are read,
    the 6th under way, the others unread."""
    pages = [(1, 30), (31, 60), (61, 90), (91, 120), (121, 142), (143, 170), (171, 200),
             (201, 230)]  # fmt: skip
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 8)
        volume = env.file("ok", units, bounds=pages)
        env.position(reader, volume, page=143, percent=143 / 230)
        assert states(env, reader, units) == ["read"] * 5 + ["in_progress", "unread", "unread"]
        assert not any(r.approximate for r in env.progress(reader).values())


@pytest.mark.req("EF-LEC-014")
def test_a_position_on_one_chapter_never_marks_the_chapters_it_skipped(tmp_path: Path) -> None:
    """The relay of a block records its position on the chapter it falls in, not on the block:
    a jump to the end of the block marks nothing read in the chapters it skipped."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 4)
        chapters = [env.file("ok", [u]) for u in units]
        env.position(reader, chapters[3], percent=1.0)  # jumped to the last chapter
        got = env.progress(reader)
        assert got[units[3]].state == "read"
        assert all(u not in got for u in units[:3])  # the others have no state at all


@pytest.mark.req("EF-LEC-014", "EF-BIB-026")
def test_position_on_replaced_content_marked_approximate(tmp_path: Path) -> None:
    """The file was replaced by a better copy (`document_alias` keeps the old hash): a position
    taken on the old one is transposed, never exact."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 2)
        item = env.file("ok", units, bounds=[(1, 10), (11, 20)], content_hash=NEW_HASH)
        env.alias(item, OLD_HASH, NEW_HASH)
        env.position(reader, item, page=15, percent=0.6, content_hash=OLD_HASH)
        got = env.progress(reader)
        assert [got[u].approximate for u in units] == [1, 1]
        assert got[units[0]].state == "read"  # by the proportion (0.6 > 0.5), not by the page
        env.position(
            reader, item, page=12, percent=0.6, content_hash=NEW_HASH,
            client_ts=hours(2), server_ts=hours(2),
        )  # fmt: skip
        got = env.progress(reader)
        assert got[units[1]].approximate == 0  # a position on the current content is exact
        assert got[units[1]].state == "in_progress"


@pytest.mark.req("EF-LEC-014")
@pytest.mark.parametrize("bounded", [True, False])
@pytest.mark.parametrize("replaced", [True, False])
@pytest.mark.parametrize(
    ("page", "percent"), [(None, None), (None, 0.0), (None, 0.4), (None, 1.0), (7, 0.2), (25, 0.9)]
)
def test_the_view_is_the_domain_rule(
    tmp_path: Path, bounded: bool, replaced: bool, page: int | None, percent: float | None
) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 3)
        pages = [(1, 10), (11, 20), (21, 30)]
        item = env.file("ok", units, bounds=pages if bounded else None, content_hash=NEW_HASH)
        env.alias(item, OLD_HASH, NEW_HASH)
        env.position(
            reader, item, page=page, percent=percent, content_hash=OLD_HASH if replaced else NEW_HASH
        )
        got = env.progress(reader)
        for rank0, unit in enumerate(units):
            want = project(
                page=page, percent=percent,
                bounds=Bounds(*(pages[rank0] if bounded else (None, None)), rank0=rank0, count=3),
                replaced=replaced,
            )  # fmt: skip
            assert (got[unit].state, bool(got[unit].approximate)) == (want.state, want.approximate)
