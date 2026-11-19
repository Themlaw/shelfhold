"""Adverse review of #387 (EF-INT-026, I39, ADR-0044): the whole instance read only once any root
says behind.

#387 put the whole-instance look in `WriteSessions.open`. The jobs, though, do not each open a
session: `Composition._hold` opens one for the first job and shares it, by a count, with every
job that starts while it is held (`write_context`, `session_opener`, `_LazyWrite`). A job that
joins a held session never runs the look. The root that comes back ahead of the database while
the session is held (a disk that was away when it opened, or a database put back under the
running instance, the case of the second review) is therefore never seen by the jobs that
follow: they write on the roots that still match for as long as the jobs overlap, which the
heartbeat allows without bound. Each write checks the lease, the persisted freeze and the
sentinel of its own root, never the others'.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.fsops.errors import RootBlockedError
from shelfhold.infrastructure.schema import platform
from tests.l41_env import Instance, instance

pytestmark = pytest.mark.req("EF-INT-026", "EF-EXP-029")

SENTINEL = ".shelfhold-mount-id"


def _roots(inst: Instance, store: str) -> Path:
    """The library root (the store is the other registered root)."""
    with inst.engine.connect() as connection:
        paths = set(connection.execute(sa.select(platform.physical_root.c.path_entry)).scalars())
    assert store in paths and len(paths) == 2
    return next(Path(p) for p in paths if p != store)


def _put_ahead(root: Path) -> None:
    """The root reads as ahead of the database: what a restore of `/data` leaves (ADR-0044)."""
    path = root / SENTINEL
    document = json.loads(path.read_text("utf-8"))
    document["generation"] = int(document["generation"]) + 5
    path.write_text(json.dumps(document), "utf-8")


def test_control_a_job_that_opens_its_own_session_refuses_once_a_root_is_ahead(
    settings: Settings, tmp_path: Path
) -> None:
    """The control: with no session held, the look of `WriteSessions.open` refuses the job."""
    with instance(settings, tmp_path) as inst:
        inst.configure()
        store = str(settings.store_dir)
        library = _roots(inst, store)
        with inst.comp.write_context():
            pass
        _put_ahead(library)
        kept = Path(store) / "kept.bin"
        with pytest.raises(RootBlockedError), inst.comp.write_context() as job:
            job.fsops.write_atomic(str(kept), b"kept", parents=True)
        assert not kept.exists()


@pytest.mark.xfail(
    strict=True,
    reason="#387 contradicteur: a job that joins the session `Composition._hold` already holds "
    "never runs the whole-instance look; it writes on the matching root while another says behind",
)
def test_a_job_that_joins_a_held_session_writes_nothing_once_a_root_is_ahead(
    settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        store = str(settings.store_dir)
        library = _roots(inst, store)
        with inst.comp.write_context():  # both roots stamped, both counters moved: healthy
            pass

        with inst.comp.write_context() as first:  # a long job: the lease is held
            assert set(first.writable_roots) == {store, str(library)}
            _put_ahead(library)  # the library comes back ahead of the database
            kept = Path(store) / "kept.bin"
            # another job starts while the first runs: `_hold` shares, no look is taken
            with pytest.raises(RootBlockedError), inst.comp.write_context() as second:
                second.fsops.write_atomic(str(kept), b"kept", parents=True)
            assert not kept.exists()  # written on a database a look would call older
