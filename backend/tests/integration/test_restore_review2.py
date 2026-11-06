"""Regression tests for the second review of PR #61 (L35)."""

import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from shelfhold.config import Settings, load_settings
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.restore import DeletionCandidate
from shelfhold.export.application.restore_purge import DeletionIncompleteError, RestoreDeletion
from shelfhold.export.domain.physical_roots import MAX_RESTORE_EPOCH, RootState
from shelfhold.library.tombstones import TombstoneRoot
from tests.auth_env import auth_env, db_rows, run_cli
from tests.integration.test_restore_epoch import purge_after_backup, restored
from tests.integration.test_restore_review import SENTINEL, edit_sentinel, stored
from tests.intents_env import NOW
from tests.restore_env import RestoreEnv, restore_env

pytestmark = pytest.mark.req("EF-INT-026")


# -- blocker 1: a forged generation ------------------------------------------------------------


@pytest.mark.parametrize("forged", [2**63, 10**30, 2**63 - 1, MAX_RESTORE_EPOCH + 1, -1, "x"])
def test_a_forged_generation_is_a_mismatch_never_a_freeze_or_an_overflow(forged: object) -> None:
    with restore_env() as env:
        edit_sentinel(env.library, generation=forged)
        status = env.guard().enter_restored_behind()
        assert not status.behind and not env.fsops.frozen
        states = {Path(f.path): f.state for f in status.findings}
        assert states[env.library] is RootState.MISMATCH
        assert stored(env) == (0, 0)


@pytest.mark.parametrize("forged", [2**63, 10**30, 2**63 - 1])
def test_a_forged_generation_does_not_break_the_exit_of_a_real_restore(forged: int) -> None:
    with restore_env() as env:
        restored(env)
        edit_sentinel(env.ex.lib.store, generation=forged)  # the other root is forged
        status = env.guard().enter_restored_behind()
        assert status.behind and 0 < status.epoch <= MAX_RESTORE_EPOCH
        adopter = env.adopter()
        adopter.adopt(confirm=adopter.preview().fingerprint)  # the +1 on the generation is bounded
        assert stored(env)[1] == 0


# -- the production composition ----------------------------------------------------------------


@contextmanager
def production(env: RestoreEnv) -> Iterator[Settings]:
    """Settings whose `/data` holds the restored database, over the same roots on disk."""
    lib = env.ex.lib
    settings = load_settings(
        {"SHELFHOLD_DATA_DIR": str(lib.data), "SHELFHOLD_STORE_DIR": str(lib.store)}
    ).values
    shutil.copyfile(env.db_path, settings.database_path)
    yield settings


def align_store_root(env: RestoreEnv, settings: Settings) -> None:
    """Only the library root is ahead: the store root's sentinel matches the restored database."""
    row = db_rows(
        settings,
        "SELECT generation FROM physical_root WHERE path_entry = ?",
        str(env.ex.lib.store),
    )[0]
    edit_sentinel(env.ex.lib.store, generation=row[0])


def test_a_root_remounted_ahead_is_frozen_through_the_real_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with restore_env() as env:
        restored(env)
        with production(env) as settings:
            align_store_root(env, settings)
            os.rename(env.library / SENTINEL, env.library / (SENTINEL + ".away"))
            with auth_env(monkeypatch, settings) as api:  # the real start, the root is away
                assert db_rows(settings, "SELECT restore_generation FROM instance") == [(0,)]
                os.rename(env.library / (SENTINEL + ".away"), env.library / SENTINEL)
                assert api.login().status_code == 200
                refused = api.post(
                    "/api/v1/plans/01J0000000000000000000PLAN/apply",
                    headers={"If-Match": '"abc"'},
                )
                assert refused.status_code == 503
                assert refused.json()["code"] == "instance.restored_behind"
                # the freeze is in the database now, and the next write is refused as well
                assert db_rows(settings, "SELECT restore_generation FROM instance")[0][0] > 0
                again = api.post(
                    "/api/v1/plans/01J0000000000000000000PLAN/apply",
                    headers={"If-Match": '"abc"'},
                )
                assert again.status_code == 503


# -- blocker 3: no freeze without a command ----------------------------------------------------


def purged_after_backup(env: RestoreEnv) -> None:
    env.add_chapters(3, 4)
    backup = env.backup()
    env.export()
    purge_after_backup(env, "4")  # the file is still on disk
    env.restore(backup)


def test_the_cli_deletes_what_a_tombstone_covers_when_confirmed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with restore_env() as env:
        purged_after_backup(env)
        with production(env) as settings:
            assert run_cli(monkeypatch, settings, "reconcile", "--adopt-after-restore") == 2
            out = capsys.readouterr().out
            fingerprint = next(
                line.split(": ")[1].strip() for line in out.splitlines() if "Empreinte" in line
            )
            before = len(env.managed())
            # without the reserved confirmation nothing is deleted, and the way out is printed
            assert (
                run_cli(
                    monkeypatch,
                    settings,
                    "reconcile",
                    "--adopt-after-restore",
                    "--confirm",
                    fingerprint,
                )
                == 0
            )
            out = capsys.readouterr().out
            assert "--confirm-deletion" in out and len(env.managed()) == before
            assert db_rows(settings, "SELECT restore_generation FROM instance")[0][0] > 0
            again = run_cli(
                monkeypatch, settings, "reconcile", "--adopt-after-restore", "--dry-run"
            )
            assert again == 0
            fingerprint = next(
                line.split(": ")[1].strip()
                for line in capsys.readouterr().out.splitlines()
                if "Empreinte" in line
            )
            code = run_cli(
                monkeypatch, settings, "reconcile", "--adopt-after-restore",
                "--confirm", fingerprint, "--confirm-deletion",
            )  # fmt: skip
            assert code == 0
            assert len(env.managed()) == before - 1  # the covered file is gone
            assert db_rows(settings, "SELECT restore_generation FROM instance") == [(0,)]


def test_an_incomplete_deletion_names_the_files_to_remove_by_hand() -> None:
    with restore_env() as env:
        victim = env.managed()[0]
        victim.unlink()
        victim.mkdir()  # not a file any more: refused, never removed
        purger = RestoreDeletion(
            env.fsops,
            IntentJournal(env.engine, env.fsops, clock=env.ex.clock),
            env.sessions.open,
            {r.root_id: r.path for r in env.loaded.roots},
        )
        candidate = DeletionCandidate(
            "d", next(r.root_id for r in env.loaded.roots if r.path == str(env.library)),
            "x/y.cbz", str(victim), "variant",
        )  # fmt: skip
        with pytest.raises(DeletionIncompleteError) as incomplete:
            purger.purge((candidate,))
        assert incomplete.value.paths == ("x/y.cbz",)


# -- minor 4 and 5 ------------------------------------------------------------------------------


def test_status_tells_what_to_fix_for_a_mismatched_root() -> None:
    from shelfhold.interfaces.cli.status import mismatch_warnings

    with restore_env() as env:
        edit_sentinel(env.library, restore_epoch="x")
        warnings = mismatch_warnings(env.guard().assess())
        assert [w["code"] for w in warnings] == ["root_sentinel_mismatch"]
        assert str(env.library) in warnings[0]["message"]
        assert ".shelfhold-mount-id" in warnings[0]["message"] and "L36" in warnings[0]["message"]


def test_a_paired_move_is_shown_in_the_preview_and_the_fingerprint() -> None:
    import hashlib

    with restore_env() as env:
        src = sorted(env.managed())[0]
        dst = src.with_name("renamed.cbz")
        journal = IntentJournal(env.engine, env.fsops, clock=env.ex.clock)
        journal.record(
            op="rename", src=str(src), dst=str(dst),
            expected_sha256="sha256:" + hashlib.sha256(src.read_bytes()).hexdigest(),
        )  # fmt: skip
        backup = env.backup()
        with env.sessions.open(str(env.library)):
            journal.resolve_open()
        env.restore(backup)
        env.guard().enter_restored_behind()
        preview = env.adopter().preview()
        assert [
            (old.rsplit("/", 1)[-1], new.rsplit("/", 1)[-1])
            for _, old, new in preview.report.relocated
        ] == [(src.name, "renamed.cbz")]
        again = env.adopter().preview()
        assert again.fingerprint == preview.fingerprint
        dst.rename(dst.with_name("another.cbz"))  # the pairing now names another file
        moved = env.adopter().preview()
        assert moved.report.relocated != preview.report.relocated
        assert moved.fingerprint != preview.fingerprint


@pytest.mark.req("EF-INT-026", "EF-BIB-053")
def test_a_damaged_journal_that_freezes_the_restore_has_an_exit() -> None:
    """Contradictor review, blocker 2: the adoption refused on a journal it could not read, and
    the only exit it named did not exist. Setting the journal aside leaves the freeze."""
    from shelfhold.export.application.restore import AdoptionBlockedError
    from shelfhold.infrastructure.fsops.tombstone_journal import TOMBSTONE_JOURNAL
    from tests.tombstone_env import ADMIN

    with restore_env() as env:
        env.add_chapters(3, 4)
        backup = env.backup()
        env.export()
        env.restore(backup)
        journal = env.library / TOMBSTONE_JOURNAL
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_bytes(b"SHELF")  # a header cut short
        env.guard().enter_restored_behind()
        assert env.fsops.frozen
        adopter = env.adopter()
        with pytest.raises(AdoptionBlockedError):
            adopter.adopt(confirm=adopter.preview().fingerprint)

        service = env.tombstones()
        here = next(r for r in env.loaded.roots if r.path == str(env.library))
        root = TombstoneRoot(here.root_id, here.path, mounted=True)
        asides = service.set_aside_journal([root], principal=ADMIN, now=NOW)  # frozen: allowed
        assert asides[here.root_id].startswith(".shelfhold/tombstones.aside-")
        assert (env.library / asides[here.root_id]).read_bytes() == b"SHELF"  # nothing deleted

        fresh = env.adopter()
        preview = fresh.preview()
        assert not preview.blocked_roots
        fresh.adopt(confirm=preview.fingerprint)
