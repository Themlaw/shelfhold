"""L31: bring the adopted files home before their mount goes (EF-EXP-051, EF-EXP-060).

The files live in a volume outside `/library`, read-only in Shelfhold (`adopt_ro`). Repatriation
copies each one, verified by hash, to the same relative path under the writable root, through the
intent journal, after a dry-run whose fingerprint it carries. Kavita keeps its series and its
chapters, and the reading progress keeps its anchors. Nothing is deleted, moved or overwritten.
"""

import pytest
import sqlalchemy as sa

from shelfhold.export.application.repatriate import (
    DryRunRequiredError,
    NotEnoughSpaceError,
    RepatriationBlockedError,
    RepatriationStaleError,
    Repatriator,
    kavita_counts,
)
from shelfhold.export.application.restore import RestoredBehindError
from shelfhold.infrastructure.fsops import ReadOnlyRootError
from shelfhold.infrastructure.schema import coherence, export, reading
from tests.adopt_env import tree_state
from tests.kavita_env import run
from tests.kavita_export_env import LIBRARY_ID, REMOTE_ROOT
from tests.repatriate_env import CHAPTERS, SERIES, Repatriation, repatriation_env

FILES = len(SERIES) * CHAPTERS


def _positions(r: Repatriation) -> list[tuple[str, str, str]]:
    with r.env.engine.connect() as connection:
        rows = connection.execute(sa.select(reading.reading_position)).all()
    return sorted((p.carrier_id, p.content_hash, str(p.locator)) for p in rows)


def _intents(r: Repatriation) -> list[sa.Row[tuple[object, ...]]]:
    return r.env.rows(coherence.fs_intent)


@pytest.mark.req("EF-EXP-051")
def test_repatriate_then_unmount_keeps_kavita_series_and_progress() -> None:
    with repatriation_env() as r:
        kavita = r.a.wired
        before_kavita = run(kavita_counts(kavita.client, str(LIBRARY_ID), REMOTE_ROOT))
        before_ids = {s["name"]: s["id"] for s in kavita.fake.series}
        before_progress = _positions(r)
        before_writes = len([w for w in kavita.fake.writes if "scan" not in w.path.lower()])
        plan = r.dry_run()
        assert (plan.series, plan.chapters, len(plan.files)) == (len(SERIES), FILES, FILES)
        assert plan.bytes_to_copy > 0 and plan.ready  # type: ignore[attr-defined]
        assert r.env.files() == []  # nothing is under /library yet
        result = r.apply(plan.fingerprint)  # type: ignore[attr-defined]
        assert (result.copied, result.already_present) == (FILES, 0)  # type: ignore[attr-defined]
        check = r.repatriator.check(r.destination, plan=plan)
        assert check.safe_to_unmount and (check.series, check.chapters) == (len(SERIES), FILES)
        r.unmount()
        # the mount is gone; the files are where Kavita sees them, byte for byte
        assert len(r.env.files()) == FILES
        assert r.repatriator.check(r.destination, plan=plan).safe_to_unmount
        # Kavita is asked to scan each folder, and indexes what is now under the writable root
        for name in SERIES:
            folder = f"{REMOTE_ROOT}/{name}"
            run(kavita.client.scan_folder(folder))
            kavita.fake.discover(
                folder,
                [f"{folder}/{p.name}" for p in sorted((r.env.root / name).iterdir())],
                library_id=LIBRARY_ID,
            )
        assert len(kavita.fake.scans) >= len(SERIES)
        after = run(kavita_counts(kavita.client, str(LIBRARY_ID), REMOTE_ROOT))
        assert after == before_kavita and after.series == len(SERIES)
        assert {s["name"]: s["id"] for s in kavita.fake.series} == before_ids
        others = [w for w in kavita.fake.writes if "scan" not in w.path.lower()]
        assert len(others) == before_writes  # only the scans we asked for, nothing else
        assert _positions(r) == before_progress  # anchors and hashes are the ones of before
        # the bookkeeping follows the files: same relative path, size and mtime of the copy
        record = r.env.rows(export.export_record)[0]
        on_disk = r.env.root / record.rel_path
        assert (record.size, record.mtime_ns) == (
            on_disk.stat().st_size,
            on_disk.stat().st_mtime_ns,
        )


@pytest.mark.req("EF-EXP-051")
def test_repatriate_refused_without_dry_run() -> None:
    with repatriation_env() as r:
        before = r.upstream_state()
        with pytest.raises(DryRunRequiredError):
            r.apply(None)
        with pytest.raises(DryRunRequiredError):
            r.apply("")
        assert r.env.files() == [] and _intents(r) == [] and r.upstream_state() == before


@pytest.mark.req("EF-EXP-051")
def test_repatriate_refused_when_the_files_changed_since_the_dry_run() -> None:
    """The fingerprint ties the apply to what the admin looked at."""
    with repatriation_env() as r:
        seen = r.dry_run().fingerprint  # type: ignore[attr-defined]
        extra = r.upstream / SERIES[0] / f"{SERIES[0]} - Ch.1.cbz"
        extra.write_bytes(extra.read_bytes() + b"x")  # the adopted file changed
        with pytest.raises(RepatriationStaleError):
            r.apply(seen)
        assert r.env.files() == [] and _intents(r) == []


@pytest.mark.req("EF-EXP-060")
def test_repatriate_reads_adopt_ro_writes_rw_root() -> None:
    with repatriation_env() as r:
        before = r.upstream_state()
        r.confirmed()
        assert r.upstream_state() == before  # the read-only root is only read
        intents = _intents(r)
        assert len(intents) == FILES
        for intent in intents:
            assert intent.op == "copy_cross_root" and intent.state == "done"
            assert intent.src_path.startswith(str(r.ro)) and intent.dst_path.startswith(
                str(r.env.root)
            )
        # a copy lands under the writable root, at the same relative path
        for record in r.env.rows(export.export_record):
            assert (r.env.root / record.rel_path).read_bytes() == (
                r.upstream / record.rel_path
            ).read_bytes()
        # and an `adopt_ro` root is never a target, not even for the repatriator's own fsops
        with pytest.raises(ReadOnlyRootError):
            r.fsops.copy_across(
                str(r.env.root / SERIES[0] / f"{SERIES[0]} - Ch.1.cbz"), str(r.ro / "x.cbz")
            )


@pytest.mark.req("EF-EXP-051")
def test_repatriate_never_overwrites_a_different_file() -> None:
    with repatriation_env() as r:
        taken = r.env.root / SERIES[0] / f"{SERIES[0]} - Ch.2.cbz"
        taken.parent.mkdir(parents=True)
        taken.write_bytes(b"somebody else's file")
        plan = r.dry_run()
        assert [f.status for f in plan.problems] == ["conflict"]  # type: ignore[attr-defined]
        with pytest.raises(RepatriationBlockedError):
            r.apply(plan.fingerprint)  # type: ignore[attr-defined]
        assert taken.read_bytes() == b"somebody else's file"
        assert len(r.env.files()) == 1 and _intents(r) == []  # nothing was copied


@pytest.mark.req("EF-EXP-051")
def test_repatriate_resumes_and_is_idempotent() -> None:
    """A run cut short is finished by the next; a finished one copies nothing again."""
    with repatriation_env() as r:
        first = r.upstream / SERIES[0] / f"{SERIES[0]} - Ch.1.cbz"
        landed = r.env.root / SERIES[0] / first.name
        landed.parent.mkdir(parents=True)
        landed.write_bytes(first.read_bytes())  # the copy landed, the bookkeeping did not
        result = r.confirmed()
        assert (result.copied, result.already_present) == (FILES - 1, 1)  # type: ignore[attr-defined]
        again = r.confirmed()
        assert (again.copied, again.already_present) == (0, FILES)  # type: ignore[attr-defined]
        assert len(_intents(r)) == FILES - 1


@pytest.mark.req("EF-EXP-051")
def test_repatriate_checks_free_space_and_missing_sources() -> None:
    with repatriation_env() as r:
        tight = Repatriator(
            r.env.engine, r.fsops, locate=r.env.lib.locate, clock=r.env.clock,
            free_bytes=lambda path: 1,
        )  # fmt: skip
        plan = tight.plan(r.destination)
        assert not plan.ready
        with pytest.raises(NotEnoughSpaceError):
            tight.apply(r.destination, fingerprint=plan.fingerprint, job_id="j")
        (r.upstream / SERIES[1] / f"{SERIES[1]} - Ch.3.cbz").unlink()
        gone = r.dry_run()
        assert [f.status for f in gone.problems] == ["missing"]  # type: ignore[attr-defined]
        with pytest.raises(RepatriationBlockedError):
            r.apply(gone.fingerprint)  # type: ignore[attr-defined]
        assert r.env.files() == []


@pytest.mark.req("EF-EXP-051", "EF-INT-026")
def test_repatriate_is_a_write_frozen_by_the_persistent_restored_behind() -> None:
    """The freeze of L35 lives in the database (`restore_generation`): it holds across a
    restart, `plan.ready` says so, and nothing is written until the exit command."""
    with repatriation_env() as r:
        with r.env.engine.begin() as connection:
            connection.execute(sa.text("UPDATE instance SET restore_generation = 1"))
        plan = r.dry_run()
        assert plan.frozen and not plan.ready  # type: ignore[attr-defined]
        with pytest.raises(RestoredBehindError) as raised:
            r.apply(plan.fingerprint)  # type: ignore[attr-defined]
        assert raised.value.code == "instance.restored_behind"
        assert r.env.files() == [] and tree_state(r.upstream) and _intents(r) == []


@pytest.mark.req("EF-EXP-051", "EF-INT-026")
def test_repatriate_root_restored_behind_is_the_catalogued_error() -> None:
    with repatriation_env(library_generation=0) as r:  # the sentinel is ahead of the database
        plan = r.dry_run()
        with pytest.raises(RestoredBehindError):
            r.apply(plan.fingerprint)  # type: ignore[attr-defined]
        assert r.env.files() == []
        assert [i.state for i in _intents(r)] == ["cancelled"]


@pytest.mark.req("EF-EXP-041")
def test_a_copy_between_roots_cut_short_is_settled_at_boot() -> None:
    from shelfhold.export.application.intents import IntentJournal

    with repatriation_env() as r:
        plan = r.dry_run()
        action = plan.files[0]  # type: ignore[attr-defined]
        journal = IntentJournal(r.env.engine, r.fsops, clock=r.env.clock)
        gone = journal.record(
            op="copy_cross_root", src=action.source, dst=action.target,
            expected_sha256=action.file_hash,
        )  # fmt: skip
        journal.mark_applying(gone)  # killed before the copy landed
        report = journal.resolve_open()
        assert report.abandoned == [gone] and not report.still_open
        done = journal.record(
            op="copy_cross_root", src=action.source, dst=action.target,
            expected_sha256=action.file_hash,
        )  # fmt: skip
        journal.mark_applying(done)
        r.fsops.copy_across(action.source, action.target, parents=True)  # landed, then killed
        report = journal.resolve_open()
        assert report.completed == [done] and not report.still_open


@pytest.mark.req("EF-EXP-051")
def test_source_changing_between_the_plan_and_the_copy_places_nothing() -> None:
    """The staged copy must have the hash of the adoption: a source rewritten after the dry-run
    leaves no file at the target (not even an orphan), and the next dry-run says `changed`."""
    with repatriation_env() as r:
        plan = r.dry_run()
        source = r.upstream / SERIES[0] / f"{SERIES[0]} - Ch.1.cbz"
        real = r.fsops.copy_across

        def racing(src: str, dst: str, **kw: object) -> str:
            if src.endswith(source.name) and not racing.done:  # type: ignore[attr-defined]
                racing.done = True  # type: ignore[attr-defined]
                source.write_bytes(source.read_bytes() + b"CHANGED")
            return real(src, dst, **kw)  # type: ignore[arg-type]

        racing.done = False  # type: ignore[attr-defined]
        r.fsops.copy_across = racing  # type: ignore[method-assign]
        with pytest.raises(RepatriationBlockedError):
            r.apply(plan.fingerprint)  # type: ignore[attr-defined]
        assert not (r.env.root / SERIES[0] / source.name).exists()
        assert [i.state for i in _intents(r)][-1] == "cancelled"
        r.fsops.copy_across = real  # type: ignore[method-assign]
        again = r.dry_run()
        assert [f.status for f in again.problems] == ["changed"]  # type: ignore[attr-defined]


@pytest.mark.req("EF-EXP-051")
def test_a_linked_destination_folder_is_a_conflict_not_a_crash() -> None:
    with repatriation_env() as r:
        outside = r.env.lib.tmp / "elsewhere"
        outside.mkdir()
        (r.env.root / SERIES[0]).symlink_to(outside)
        plan = r.dry_run()
        assert {f.status for f in plan.problems} == {"conflict"}  # type: ignore[attr-defined]
        with pytest.raises(RepatriationBlockedError):
            r.apply(plan.fingerprint)  # type: ignore[attr-defined]
        assert list(outside.iterdir()) == []


@pytest.mark.req("EF-EXP-051")
def test_check_compares_the_counts_with_the_dry_run() -> None:
    with repatriation_env() as r:
        plan = r.dry_run()
        r.apply(plan.fingerprint)  # type: ignore[attr-defined]
        assert r.repatriator.check(r.destination, plan=plan).matches_plan  # type: ignore[arg-type]
        with r.env.engine.begin() as connection:
            connection.execute(sa.text("DELETE FROM export_record WHERE rel_path LIKE '%Ch.3%'"))
        check = r.repatriator.check(r.destination, plan=plan)  # type: ignore[arg-type]
        assert not check.matches_plan and not check.safe_to_unmount
