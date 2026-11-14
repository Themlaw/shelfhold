"""Contradictor of #239 (M3 L29): proofs of concept, red until the objections are answered.

Each test attacks one promise of the rename plans (EF-EXP-030, EF-EXP-037, EF-EXP-041, I9): a
batch left half applied, a write under an open intent, an intent left open by a refused rename, a
name Kavita and SMB take for another, a plan applied by two jobs at once, a key that replays the
application, and the reader count in a log line. See `docs/decisions/M3.md`, "L29 : contradicteur
de la #239"."""

import contextlib
import errno
import re
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.rename_plan import (
    ItemMove,
    RenameConfirmationRequiredError,
    RenameRefusedError,
)
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.logging import exception_detail
from shelfhold.infrastructure.schema import platform
from tests.auth_env import auth_env, run_cli
from tests.export_env import ExportEnv, export_env
from tests.ops_env import job_status, make_ops
from tests.rename_env import (
    FakeReaders,
    export_chapters,
    item_ids_of,
    open_intents,
    plan_status,
    record_paths,
    tree,
)

KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")


@pytest.fixture
def shelf() -> Iterator[ExportEnv]:  # not `env`: `settings` already takes a fixture of that name
    with export_env() as made:
        yield made


def _apply(env: ExportEnv, preview: Any, *, confirm: bool = False, job: str | None = None) -> None:
    env.pipeline.apply(
        preview.plan_id, if_match=preview.base_version, job_id=job or new_ulid(),
        confirm_progress_loss=confirm,
    )  # fmt: skip


def _move_series(env: ExportEnv, folder: str, new_folder: str) -> list[ItemMove]:
    return [
        ItemMove(item_id, f"{new_folder}/{name}")
        for name, item_id in item_ids_of(env, folder).items()
    ]


# -- B1: a chain of renames inside one batch -----------------------------------------------------


@pytest.mark.req("EF-EXP-030", "EF-EXP-041")
def test_poc_chain_inside_a_batch_is_refused_or_fully_applied(shelf: ExportEnv) -> None:
    env = shelf
    """`_free_file(vacated=...)` lets a step target the old name of a later step of the same batch;
    the steps run in the order of the old paths and the rename never replaces: the batch stops
    half way (no scan, plan `failed`) and the refused step leaves its intent `applying`."""
    export_chapters(env, "Dr Stone", ["1", "2", "3"])
    ids = item_ids_of(env, "Dr Stone")
    moves = [
        ItemMove(ids["Dr Stone - Ch.1.cbz"], "Dr Stone/Z.cbz"),  # runs first, succeeds
        ItemMove(ids["Dr Stone - Ch.2.cbz"], "Dr Stone/Dr Stone - Ch.3.cbz"),  # Ch.3 still there
        ItemMove(ids["Dr Stone - Ch.3.cbz"], "Dr Stone/Y.cbz"),
    ]
    try:
        preview = env.pipeline.rename_preview(env.destination_id, moves=moves)
    except RenameRefusedError:
        return  # refusing an order the disk cannot follow is an acceptable answer
    env.scans.clear()
    refused: RenameRefusedError | None = None
    try:
        _apply(env, preview)
    except RenameRefusedError as error:  # PoC: "destination_taken" after step 1
        refused = error
    assert refused is None, (refused, tree(env), open_intents(env), len(env.scans))
    assert tree(env) == ["Dr Stone/Dr Stone - Ch.3.cbz", "Dr Stone/Y.cbz", "Dr Stone/Z.cbz"]
    assert record_paths(env) == tree(env) and open_intents(env) == 0
    assert plan_status(env, preview.plan_id) == "done" and len(env.scans) == 1


# -- B2: a whole series folder moved over a path an open intent covers (I9) ---------------------


@pytest.mark.req("EF-EXP-030", "EF-EXP-041")
def test_poc_series_folder_holding_an_open_intent_is_not_moved(shelf: ExportEnv) -> None:
    env = shelf
    """`within_any(step.src, covered)` asks whether the folder lies *under* an open intent, never
    whether an open intent lies under the folder: a write in flight inside the series folder (or
    an intent left open as drift) is carried away by the folder rename."""
    export_chapters(env, "Dr Stone", ["1", "2"])
    in_flight = str(env.root / "Dr Stone" / "Dr Stone - Ch.3.cbz")
    IntentJournal(env.engine, env.lib.fsops).record(
        op="write", src=in_flight + ".part", dst=in_flight
    )
    moves = _move_series(env, "Dr Stone", "Dr Stone (2019)")
    with pytest.raises(RenameRefusedError) as refused:
        env.pipeline.rename_preview(env.destination_id, moves=moves)
    assert refused.value.reason == "intent_open"


# -- M1: a rename the disk refuses (EXDEV, EBUSY, EACCES) leaves its intent open ---------------


@pytest.mark.req("EF-EXP-030", "EF-EXP-041")
@pytest.mark.parametrize("code", [errno.EXDEV, errno.EBUSY, errno.EACCES])
def test_poc_a_folder_rename_the_disk_refuses_closes_its_intent(
    shelf: ExportEnv, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """`apply_move_dir` cancels its intent on `FileExistsError` only. A series folder that is a
    mount point (EXDEV/EBUSY) or a parent without write right (EACCES) leaves an intent
    `applying`: every later plan on the series answers `intent_open`, and the boot replays the
    same failing rename at each start."""
    env = shelf
    export_chapters(env, "Dr Stone", ["1"])
    preview = env.pipeline.rename_preview(
        env.destination_id, moves=_move_series(env, "Dr Stone", "Dr Stone (2019)")
    )
    real = env.lib.fsops.rename

    def refuse(src: str, dst: str, **kwargs: Any) -> None:
        if Path(src).name == "Dr Stone":
            raise OSError(code, "refused by the disk")
        real(src, dst, **kwargs)

    monkeypatch.setattr(env.lib.fsops, "rename", refuse)
    with pytest.raises((OSError, RenameRefusedError)):
        _apply(env, preview)
    assert (env.root / "Dr Stone" / "Dr Stone - Ch.1.cbz").is_file()
    assert open_intents(env) == 0  # PoC: 1, state `applying`
    monkeypatch.setattr(env.lib.fsops, "rename", real)
    again = env.pipeline.rename_preview(  # the series can be planned again
        env.destination_id, moves=_move_series(env, "Dr Stone", "Dr Stone (2019)")
    )
    assert again.plan_id is not None


# -- M2: a name Kavita and SMB take for another one ---------------------------------------------


@pytest.mark.req("EF-EXP-030", "EF-EXP-045")
@pytest.mark.parametrize(
    "new_folder",
    [
        unicodedata.normalize("NFD", "Café"),  # "Café" (NFC) is already on the disk
        "Dr Stone.",  # trailing dot: refused by `sanitize_name`, lost on SMB
        "Dr: Stone",  # forbidden on SMB
    ],
)
def test_poc_a_move_names_like_an_export_would(shelf: ExportEnv, new_folder: str) -> None:
    """`validate_rel_path` checks `..`, control characters and the reserved area, but never NFC nor
    `sanitize_name`: a reshelving makes a folder that no export could make, and an NFD name next
    to its NFC twin passes the collision check (`_fold` does not normalise)."""
    env = shelf
    export_chapters(env, "Dr Stone", ["1"])
    (env.root / unicodedata.normalize("NFC", "Café")).mkdir()
    with pytest.raises(RenameRefusedError) as refused:
        env.pipeline.rename_preview(
            env.destination_id, moves=_move_series(env, "Dr Stone", new_folder)
        )
    assert refused.value.reason in ("destination_taken", "invalid_path")


# -- M3: the same plan applied by two jobs at once ---------------------------------------------


@pytest.mark.req("EF-EXP-030", "EF-EXP-042")
def test_poc_a_plan_being_applied_is_not_applied_again_by_another_job(shelf: ExportEnv) -> None:
    env = shelf
    """`_apply_rename` takes any plan `applying` with the right validator for a plan cut short and
    resumes it, even while its first job is still running; `apply_key` makes the confirmation part
    of the dedupe key, so a second request with another flag gets a second job. The settle of the
    second job resolves the intents of the first one under its feet."""
    export_chapters(env, "Dr Stone", ["1"])
    export_chapters(env, "Blue Box", ["1"])
    moves = _move_series(env, "Dr Stone", "Dr Stone (2019)") + _move_series(
        env, "Blue Box", "Blue Box (2021)"
    )
    preview = env.pipeline.rename_preview(env.destination_id, moves=moves)
    assert preview.batches == 2
    second: list[tuple[str, str]] = []
    book = env.pipeline._renames
    book._lock_wait_s = 0.5
    first_scan = book._request_scan

    def scan(folder: str) -> None:  # batch 1 is done, the first job is still in the plan
        if first_scan is not None:
            first_scan(folder)
        if second:
            return
        try:
            _apply(env, preview, job="01JJOB00000000000000SECOND")
        except Exception as error:  # what the second job met, whatever it is
            second.append((type(error).__name__, plan_status(env, preview.plan_id)))
        else:
            second.append(("applied", plan_status(env, preview.plan_id)))

    book._request_scan = scan
    with contextlib.suppress(StalePlanError):  # the first job may find the data moved under it
        _apply(env, preview)
    # the second job is refused at once and leaves the plan to the first one, which completes it
    assert second == [("StalePlanError", "applying")]  # PoC: [("LockBusyError", "failed")]
    assert plan_status(env, preview.plan_id) == "done"


# -- M4: an admin key replays the application of a rename plan ----------------------------------


@pytest.mark.req("EF-EXP-030", "EF-EXP-037", "EF-USR-024")
def test_poc_a_key_cannot_replay_the_job_that_applies_a_rename_plan(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    """`POST /plans/{id}/apply` is the reserved action `plan.apply_rename` (fresh admin session,
    never a key); `POST /ops/jobs/{id}/retry` takes an admin key and puts back a dead apply job
    whose dedupe key still says `:confirmed`: the plan is applied (or resumed) later, without a
    fresh session and on a confirmation given for another count."""
    from shelfhold.runtime_jobs import APPLY_JOB_KIND, apply_key

    with export_env() as env:
        export_chapters(env, "Dr Stone", ["1"])
        preview = env.pipeline.rename_preview(
            env.destination_id, moves=_move_series(env, "Dr Stone", "Dr Stone (2019)")
        )
        job_id = new_ulid()
        with env.engine.begin() as conn:
            conn.execute(
                sa.insert(platform.job_view).values(
                    id=job_id, kind=APPLY_JOB_KIND, status="dead_letter",
                    dedupe_key=apply_key(preview.plan_id, preview.base_version, confirmed=True),
                    error_code="job.failed", last_error="OSError", attempts=5,
                    huey_task_id=new_ulid(),
                )
            )  # fmt: skip
        ops = make_ops(env)
        with auth_env(monkeypatch, settings, ops_services=ops) as api:
            capsys.readouterr()
            created = run_cli(monkeypatch, api.settings, "admin", "keys", "create", "--name", "ci")
            assert created == 0
            key = KEY.search(capsys.readouterr().out)
            assert key
            bearer = {"Authorization": f"Bearer {key.group(0)}"}
            answer = api.client.post(f"/api/v1/jobs/{job_id}/retry", headers=bearer)
            assert answer.status_code == 403  # PoC: 200
            assert job_status(env.engine, job_id) == "dead_letter"  # PoC: `queued`


# -- m1: the confirmation is not tied to the count it confirms ----------------------------------


@pytest.mark.req("EF-EXP-037")
def test_poc_the_confirmation_is_for_the_count_that_was_shown(shelf: ExportEnv) -> None:
    env = shelf
    """`confirm_progress_loss` is a bare boolean: the admin confirmed "1 reader" on the preview,
    5 readers now lose their progress and the plan applies on the old confirmation."""
    export_chapters(env, "Dr Stone", ["1"])
    readers = FakeReaders(count=1)
    env.pipeline.bind_readers(readers)
    preview = env.pipeline.rename_preview(
        env.destination_id, moves=_move_series(env, "Dr Stone", "Dr Stone (2019)")
    )
    assert preview.warnings[0].readers == 1
    readers.count = 5
    with pytest.raises(RenameConfirmationRequiredError):
        _apply(env, preview, confirm=True)  # PoC: applied


# -- m2: the reader count in a log line ----------------------------------------------------------


@pytest.mark.req("EF-EXP-037")
def test_poc_the_reader_count_is_not_in_the_message_a_failed_job_logs() -> None:
    """The job runner logs `exception_detail(exc)` (class and message) when a job raises; the
    application re-asks the count in the job, and the refusal's message is `"N readers"`."""
    detail = exception_detail(RenameConfirmationRequiredError(7, exact=True))
    assert "7" not in str(detail["message"])  # PoC: "'7 readers'"
