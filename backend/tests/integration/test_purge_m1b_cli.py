"""EF-BIB-052, EF-BIB-053, EF-SRC-033: `shelfhold purge` as the admin runs it (L17).

The command itself, over an instance built by the production services (the follow cycle, the
simulated Suwayomi and Kavita): nothing here stubs the channel, the socket or the service."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, platform
from shelfhold.ops.backup.store import archive_name
from tests.integration.test_l53_status import _paired
from tests.l54_env import Bench54, bench54

pytestmark = pytest.mark.req("EF-BIB-052", "EF-BIB-053", "EF-SRC-033")


def _work_of(b: Bench54, edition: str) -> str:
    with b.engine.connect() as connection:
        return str(
            connection.execute(
                sa.text("SELECT work_id FROM edition WHERE id = :e"), {"e": edition}
            ).scalar_one()
        )


def _suwayomi(b: Bench54) -> dict[str, object]:
    return b.env.inst.suwayomi.state["mangas"][0]  # type: ignore[no-any-return]


def test_purge_now_work_unfollows_releases_and_lists_the_retained_backups(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    folder = tmp_path / "backups"
    folder.mkdir()
    monkeypatch.setenv("SHELFHOLD_BACKUP_DIR", str(folder))
    with bench54(monkeypatch, settings, tmp_path) as b:
        work = _work_of(b, _paired(b, capsys))
        assert _suwayomi(b)["inLibrary"] is True
        name = archive_name("20990101T000000Z")
        (folder / name).write_bytes(b"shelfhold-backup/2 1\n")
        with b.engine.begin() as connection:
            connection.execute(
                sa.insert(platform.backup_run).values(
                    id=new_ulid(), started_at=datetime(2099, 1, 1, tzinfo=UTC),
                    finished_at=datetime(2099, 1, 1, tzinfo=UTC), state="ok", file_name=name,
                )
            )  # fmt: skip
        capsys.readouterr()
        assert b.cli("purge", "--now", "--work", work, "--confirm", work) == 0
        shown = capsys.readouterr()
        assert _suwayomi(b)["inLibrary"] is False  # the series left the service's library
        assert re.search(r"1 série\(s\) retirée\(s\)", shown.out)
        assert name in shown.err  # a retained backup that still holds the title: said, not touched
        assert (folder / name).read_bytes() == b"shelfhold-backup/2 1\n"


def test_purge_unit_refuses_without_the_typed_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        work = _work_of(b, _paired(b, capsys))
        assert b.cli("purge", "--now", "--work", work) == 2
        assert b.cli("purge", "--work", work, "--confirm", work) == 2
        assert b.cli("purge", "--now", "--work", work, "--confirm", "wrong") != 0
        assert _suwayomi(b)["inLibrary"] is True  # nothing was touched


def test_leftovers_plan_then_confirmation_only_by_the_version_read(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """The migration offers (a plan, nothing erased) and applies only with the version it printed:
    the series a purge buried and the service still lists, and the tickets it left."""
    with bench54(monkeypatch, settings, tmp_path) as b:
        work = _work_of(b, _paired(b, capsys))
        with (
            b.engine.begin() as connection
        ):  # a request left by a purge of M1a: closed, ticket kept
            request = connection.execute(
                sa.select(acquisition.download_request.c.id).where(
                    acquisition.download_request.c.ticket.is_not(None)
                )
            ).first()
            if request is not None:
                connection.execute(
                    sa.update(acquisition.download_request)
                    .where(acquisition.download_request.c.id == request.id)
                    .values(state="closed_by_purge", variant_id=None)
                )
        assert b.cli("purge", "--now", "--work", work, "--confirm", work) == 0
        _suwayomi(b)["inLibrary"] = True  # the purge of M1a left the series in the library
        capsys.readouterr()
        assert b.cli("purge", "--leftovers") == 0
        planned = capsys.readouterr().out
        match = re.search(r"--confirm (\w+)", planned)
        assert match and "1 série(s)" in planned
        assert _suwayomi(b)["inLibrary"] is True  # a plan reads only
        assert b.cli("purge", "--leftovers", "--confirm", "0" * 32) != 0  # a stale plan
        assert _suwayomi(b)["inLibrary"] is True
        capsys.readouterr()
        assert b.cli("purge", "--leftovers", "--confirm", match.group(1)) == 0
        assert "1 série(s) retirée(s)" in capsys.readouterr().out
        assert _suwayomi(b)["inLibrary"] is False


def test_leftovers_without_a_connected_service_offers_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    from tests.auth_env import run_cli
    from tests.l50_env import seeded

    with seeded(settings, tmp_path):
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "purge", "--leftovers") == 0
        assert "Aucun service connecté" in capsys.readouterr().out


def test_kept_in_service_is_shown_by_status_until_the_admin_clears_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """A download the service still holds after a purge (another account keeps it) is a durable
    mark: `status` shows it, and only `purge --kept-ack` clears it."""
    from shelfhold.library.public import KEPT_PREFIX

    with bench54(monkeypatch, settings, tmp_path) as b:
        request = new_ulid()
        with b.engine.begin() as connection:
            connection.execute(
                sa.insert(platform.app_setting).values(
                    key=KEPT_PREFIX + request, value="2099-01-01T00:00:00+00:00"
                )
            )
        capsys.readouterr()
        b.cli("status")
        assert request in capsys.readouterr().out
        assert b.cli("purge", "--kept-ack") == 0
        assert "1 marque(s)" in capsys.readouterr().out
        with b.engine.connect() as connection:  # a reserved action: the trace is in the audit log
            acted = connection.execute(
                sa.text("SELECT count(*) FROM audit WHERE action = 'purge.kept_acknowledged'")
            ).scalar_one()
        assert acted == 1
        b.cli("status")
        assert request not in capsys.readouterr().out


def test_purge_sweeps_the_backup_snapshots_a_dead_run_left_behind(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """A killed backup leaves `/data/tmp/backup-*` (a database snapshot, titles in the clear):
    the report of the retained backups clears what nobody holds before it lists."""
    from shelfhold.ops.backup.scratch import MARKER, SCRATCH_PARENT

    folder = tmp_path / "backups"
    folder.mkdir()
    monkeypatch.setenv("SHELFHOLD_BACKUP_DIR", str(folder))
    with bench54(monkeypatch, settings, tmp_path) as b:
        work = _work_of(b, _paired(b, capsys))
        orphan = settings.data_dir / SCRATCH_PARENT / "backup-deadbeef"
        orphan.mkdir(parents=True)
        (orphan / MARKER).write_bytes(b"")
        (orphan / "snapshot.db").write_bytes(b"a title in the clear")
        assert b.cli("purge", "--now", "--work", work, "--confirm", work) == 0
        assert not orphan.exists()


def test_purge_checkpoint_redoes_only_the_log_and_lifts_the_status_mark(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    from shelfhold.infrastructure.wal import WAL_PENDING_KEY

    with bench54(monkeypatch, settings, tmp_path) as b:
        with b.engine.begin() as connection:
            connection.execute(
                sa.insert(platform.app_setting).values(
                    key=WAL_PENDING_KEY, value="2099-01-01T00:00:00+00:00"
                )
            )
        capsys.readouterr()
        b.cli("status")
        assert "shelfhold purge --checkpoint" in capsys.readouterr().out
        assert b.cli("purge", "--checkpoint") == 0
        assert "finalisé" in capsys.readouterr().out
        b.cli("status")
        assert "purge --checkpoint" not in capsys.readouterr().out
