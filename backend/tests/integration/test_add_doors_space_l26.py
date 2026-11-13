"""M3 L26 (EF-BIB-030): « add refused over free space » on every door that writes into /data or
/store, on the server and before the first write. A volume at its reserve is faked (the real disk
of the test is not touched); each door must refuse with the coded 409 and the figures, and leave
the database as it was. The same doors work when the room is there."""

from pathlib import Path
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.import_jobs import import_handler
from shelfhold.infrastructure.observability.disk_guard import GIB, Usage
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.ops import reclaim
from shelfhold.ops.acquire import AcquireOps
from shelfhold.ops.backfill import BackfillOps
from shelfhold.ops.errors import CodedRefusal
from shelfhold.ops.follows import FollowOps
from shelfhold.ops.import_folder import ImportOps
from shelfhold.ops.requests import build_ports
from tests.auth_env import db_dump, run_cli
from tests.l41_env import context, instance
from tests.l50_env import seeded

pytestmark = pytest.mark.req("EF-BIB-030")

CODE = "library.space_insufficient"


MARKS = {
    "disk_pause_percent": 5.0, "disk_pause_gib": 10.0,
    "disk_resume_percent": 8.0, "disk_resume_gib": 15.0,
}  # fmt: skip


def _volume(monkeypatch: pytest.MonkeyPatch, free: int, total: int = 100 * GIB) -> None:
    """A volume of `total` bytes with `free` left. The tests run with the pause marks at zero (the
    real disk of the machine is not a variable): the marks of a real instance are put back."""
    monkeypatch.setattr(reclaim, "disk_usage", lambda _folder: Usage(total, free))


def _marked(comp: Any) -> None:
    comp.settings = comp.settings.model_copy(update=MARKS)


def _refused(call: Any) -> CodedRefusal:
    with pytest.raises(CodedRefusal) as raised:
        call()
    assert raised.value.code == CODE
    return raised.value


def test_the_gate_names_the_figures_and_the_action_and_refuses_a_volume_at_its_reserve(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        _marked(inst.comp)
        _volume(monkeypatch, free=8 * GIB)  # under the 10 GiB reserve: nothing fits, not a byte
        refused = _refused(lambda: reclaim.require_space(inst.comp))
        assert refused.extensions is not None
        assert refused.extensions["reclaim_action"] == "empty_trash"
        assert refused.extensions["free_bytes"] == 8 * GIB
        assert refused.extensions["reserve_bytes"] == 10 * GIB
        _volume(monkeypatch, free=40 * GIB)
        reclaim.require_space(inst.comp)  # unknown size, room left: it goes
        reclaim.require_space(inst.comp, 20 * GIB)
        _refused(lambda: reclaim.require_space(inst.comp, 31 * GIB))  # 40 - 10 reserve = 30 usable


def test_the_http_doors_answer_a_409_with_the_figures(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        _marked(inst.comp)
        _volume(monkeypatch, free=1 * GIB)
        with pytest.raises(ProblemError) as problem, refusals():
            reclaim.require_space(inst.comp, 5 * GIB)
        assert problem.value.status == 409
        assert problem.value.extensions is not None
        assert problem.value.extensions["estimated_bytes"] == 5 * GIB


def test_acquire_refuses_before_recording_and_before_queueing(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        _marked(inst.comp)
        inst.configure()
        ops = AcquireOps(inst.comp)
        queued: list[str] = []

        class _Pools:
            def submit(self, kind: str, **_: object) -> Any:
                queued.append(kind)
                return type("Job", (), {"job_id": "j"})()

        monkeypatch.setattr(inst.comp, "pools", lambda: _Pools())
        before = db_dump(settings)
        _volume(monkeypatch, free=2 * GIB)

        _refused(
            lambda: ops.prepare(
                url=None, source_key="source-a", remote_id="1", unit=None, requester_user_id=None
            )
        )
        _refused(lambda: ops.enqueue("01J0000000000000000000REQ1"))
        _refused(
            lambda: ops.acquire(
                url=None, source_key="source-a", remote_id="1", unit=None, requester_user_id=None
            )
        )

        assert queued == [] and db_dump(settings) == before  # no row, no job
        _volume(monkeypatch, free=50 * GIB)
        assert ops.enqueue("01J0000000000000000000REQ1")["state"] == "queued"
        assert len(queued) == 1


def test_the_approval_of_a_request_is_refused_through_its_ports(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """« Approuver » prepares the Work and queues the download: both come through the ports,
    which come through the same doors, so a full volume refuses the approval, before it is
    claimed (the request stays pending)."""
    with instance(settings, tmp_path) as inst:
        _marked(inst.comp)
        inst.configure()
        ports = build_ports(inst.comp)
        _volume(monkeypatch, free=2 * GIB)
        before = db_dump(settings)
        _refused(lambda: ports.prepare("source-a", "1", requester_user_id=None, visibility="ops"))
        _refused(lambda: ports.follow("source-a", "1", requester_user_id=None, visibility="ops"))
        _refused(lambda: ports.start("01J0000000000000000000REQ1"))
        assert db_dump(settings) == before


def test_follow_refuses_before_any_write(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        _marked(inst.comp)
        inst.configure()
        before = db_dump(settings)
        _volume(monkeypatch, free=2 * GIB)
        _refused(
            lambda: FollowOps(inst.comp).follow(
                "source-a", "1", requester_user_id=None, visibility="ops"
            )
        )
        assert db_dump(settings) == before


def test_backfill_start_refuses_on_its_own_estimate_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        _marked(inst.comp)
        started: list[str] = []

        class _Estimate:
            mode, units, size_bytes, free_bytes, needs_confirmation = "gaps", 3, 25 * GIB, 0, False

        class _Service:
            def estimate(self, *_a: object) -> _Estimate:
                return _Estimate()

            def start(self, *_a: object, **_k: object) -> _Estimate:
                started.append("start")
                return _Estimate()

        ops = BackfillOps(inst.comp)
        monkeypatch.setattr(ops, "_service", lambda *_a: (_Service(), "gaps"))
        _volume(monkeypatch, free=30 * GIB)  # 20 GiB usable < 25 GiB estimated

        refused = _refused(lambda: ops.start("e1", "gaps", confirm_units=None, confirm_bytes=None))

        assert refused.extensions is not None and refused.extensions["estimated_bytes"] == 25 * GIB
        assert started == []


def test_the_import_scan_and_the_tree_import_refuse_but_a_dry_run_reads_on(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    folder = tmp_path / "incoming"
    folder.mkdir()
    (folder / "one.cbz").write_bytes(b"not read")
    monkeypatch.setenv("SHELFHOLD_IMPORT_DIR", str(folder))
    for name, value in MARKS.items():  # the CLI reads its own settings
        monkeypatch.setenv(f"SHELFHOLD_{name.upper()}", str(value))
    with seeded(settings, tmp_path / "inst") as env:
        env.inst.comp.settings = settings.model_copy(update={"import_dir": folder, **MARKS})
        ops = ImportOps(env.inst.comp)
        _volume(monkeypatch, free=3 * GIB)

        _refused(lambda: ops.scan())
        tree = tmp_path / "tree"
        tree.mkdir()
        _refused(lambda: ops.tree(str(tree)))
        assert ops.scan(dry_run=True)["dry_run"] is True  # reads, writes nothing
        assert (folder / "one.cbz").exists()  # nothing was taken in

        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "import", "scan") != 0  # the CLI door says it too
        assert CODE in capsys.readouterr().err

        context_ = context("import.scan")
        import_handler(env.inst.comp)(context_)  # the job waits for room, it does not fail
        assert (folder / "one.cbz").exists()
