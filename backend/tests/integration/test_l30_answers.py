"""L30, the answers to the counter-review (M3, EF-EXP-031): the key goes by its identifier and a
stop after it is finished by the same command, one lock per destination excludes every export,
nothing is deleted that is not verified again just before, a library gone is said and not an
obstacle, giving up after the point of no return is refused, the erase log and the library's own
root are protected, and the copy rows of another destination are never forgotten."""

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.detach import DetachRefusedError, FullDetacher, full_detach_phrase
from shelfhold.export.application.locks import LockBusyError, ResourceLocks
from shelfhold.export.application.remove_destination import RemovalMode, removal_phrase
from shelfhold.export.domain.physical_roots import SENTINEL_FILE, TOMBSTONE_JOURNAL
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.fsops.confined import ConfinedRoot
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import coherence, export
from shelfhold.infrastructure.schema import replica as replica_schema
from tests.detach_env import ADMIN, DetachEnv, detach_env
from tests.export_env import add_second_shelf
from tests.fakes.kavita import API_KEY, OTHER_KEY
from tests.rename_env import export_chapters

pytestmark = pytest.mark.req("EF-EXP-031")


class Killed(BaseException):
    """`kill -9`, seen from inside: nothing after it runs."""


@pytest.fixture
def d() -> Iterator[DetachEnv]:
    with detach_env() as made:
        export_chapters(made.env, "Dr Stone", ["1", "2", "3"])
        yield made


def _clean_bins(d: DetachEnv) -> None:
    for name in ("recycle", "received"):
        shutil.rmtree(d.root / ".shelfhold" / name, ignore_errors=True)
    shutil.rmtree(d.env.lib.manga / ".shelfhold", ignore_errors=True)


def _detach(d: DetachEnv, **kw: Any) -> Any:
    target = d.env.target_id
    return d.detacher.apply(
        target, principal=ADMIN, typed=full_detach_phrase(target), client=d.wired.client,
        revoker=kw.pop("revoker", d.revoker), credential="the account password", **kw,
    )  # fmt: skip


def _remove(d: DetachEnv, mode: RemovalMode, dest: str | None = None) -> Any:
    dest = dest or d.env.destination_id
    return d.remover.apply(dest, mode, principal=ADMIN, typed=removal_phrase(dest))


def _series_folder(d: DetachEnv) -> str:
    return Path(d.env.files()[0]).relative_to(d.env.root).parts[0]


def _codes(report: Any) -> set[str]:
    return {line.code for line in report.leftovers}


def _revocations(d: DetachEnv) -> list[Any]:
    return [
        row for row in d.intents()
        if isinstance(row.expected_file_id, dict)
        and row.expected_file_id.get("scope") == "revoke_key"
    ]  # fmt: skip


def _copies(d: DetachEnv) -> list[Any]:
    table = replica_schema.replica
    with d.env.engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(table).where(table.c.location == "destination").order_by(table.c.rel_path)
            )
        )


# -- B1 : the key goes by its identifier -----------------------------------------------------------


def test_key_is_written_by_identifier_before_it_goes_and_never_by_name(d: DetachEnv) -> None:
    _clean_bins(d)
    seen: list[Any] = []
    d.revoker.seen_before = lambda: seen.append(_revocations(d))
    assert _detach(d).complete
    (rows,) = seen
    (row,) = rows
    assert row.state == "applying"  # on record, durably, before the deed
    assert row.expected_file_id["key_id"] == "1"
    written = json.dumps(row.expected_file_id)
    assert API_KEY not in written and "shelfhold" not in written.lower().replace("scope", "")
    assert d.revoker.ids == ["1"]
    assert {k["id"] for k in d.wired.fake.auth_keys} == {2}  # its own, and nobody else's


def test_a_stop_after_the_key_is_revoked_is_finished_with_a_strict_kavita(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The simulated Kavita refuses the revoked key (401): the rerun must not use it, neither to
    give the settings back again nor to find the key again."""
    _clean_bins(d)

    def die(*_a: Any, **_k: Any) -> Any:
        raise Killed

    monkeypatch.setattr(FullDetacher, "_forget", die)
    with pytest.raises(Killed):
        _detach(d)
    monkeypatch.undo()
    assert not d.wired.fake.key_is_valid(API_KEY)  # gone for good in Kavita
    assert d.kavita_patterns() == ["*.tmp"]  # and the settings were given back before
    d.revoker.calls.clear()
    report = _detach(d)
    assert report.complete and report.key_revoked
    assert d.count(export.shelf_destination) == 0
    assert d.wired.fake.key_is_valid(OTHER_KEY)  # another key of the account is never touched


def test_a_stop_before_the_key_went_revokes_it_by_the_recorded_identifier(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_bins(d)
    d.revoker.raises = Killed()
    with pytest.raises(Killed):
        _detach(d)
    d.revoker.raises = None
    (row,) = _revocations(d)
    assert row.state == "applying" and row.expected_file_id["key_id"] == "1"
    assert d.wired.fake.key_is_valid(API_KEY)  # not revoked yet
    assert _detach(d).complete
    assert d.revoker.ids == ["1"]


def test_a_key_that_took_the_name_of_the_dedicated_one_is_never_revoked(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rerun finds the dedicated key gone. A key made by hand since, with the same name, is not
    it: the identifier is what counts, so the other key stays."""
    _clean_bins(d)
    monkeypatch.setattr(FullDetacher, "_forget", lambda *_a, **_k: (_ for _ in ()).throw(Killed()))
    with pytest.raises(Killed):
        _detach(d)
    monkeypatch.undo()
    d.wired.fake.auth_keys.append({"id": 3, "name": "shelfhold", "key": "synthetic-admin-key-0003"})
    assert _detach(d).complete
    assert d.wired.fake.key_is_valid("synthetic-admin-key-0003")
    assert d.revoker.ids == ["1"]


def test_a_key_that_cannot_be_named_is_not_revoked_and_nothing_is_recorded(d: DetachEnv) -> None:
    _clean_bins(d)
    d.revoker.key_id = lambda *_a, **_k: None  # type: ignore[method-assign]
    report = _detach(d)
    assert not report.complete and "key_not_revoked" in _codes(report)
    assert _revocations(d) == [] and d.count(export.shelf_destination) == 1


# -- B2 : one lock per destination, both ways ------------------------------------------------------


def test_removal_holds_the_destination_alone_and_an_export_waits(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks = ResourceLocks(d.env.engine, clock=d.env.clock)
    dest = d.env.destination_id
    series = f"{dest}/{_series_folder(d)}"
    seen: list[str] = []
    real = Fsops.purge_unlink

    def spy(self: Fsops, local: str, **kw: Any) -> None:
        try:
            with locks.hold(new_ulid(), work=new_ulid(), series=series):
                seen.append("export got in")
        except LockBusyError:
            seen.append("busy")
        real(self, local, **kw)

    monkeypatch.setattr(Fsops, "purge_unlink", spy)
    assert _remove(d, RemovalMode.DELETE_FILES).complete
    assert set(seen) == {"busy"}


def test_detach_holds_the_destinations_alone_and_an_export_waits(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_bins(d)
    locks = ResourceLocks(d.env.engine, clock=d.env.clock)
    series = f"{d.env.destination_id}/{_series_folder(d)}"
    seen: list[str] = []
    real = Fsops.erase_host_area

    def spy(self: Fsops, root: str, **kw: Any) -> None:
        try:
            with locks.hold(new_ulid(), work=new_ulid(), series=series):
                seen.append("export got in")
        except LockBusyError:
            seen.append("busy")
        real(self, root, **kw)

    monkeypatch.setattr(Fsops, "erase_host_area", spy)
    assert _detach(d).complete
    assert seen == ["busy"]


def test_exports_of_one_destination_do_not_exclude_each_other(d: DetachEnv) -> None:
    locks = ResourceLocks(d.env.engine, clock=d.env.clock)
    dest = d.env.destination_id
    with (
        locks.hold(new_ulid(), work=new_ulid(), series=f"{dest}/Series A"),
        locks.hold(new_ulid(), work=new_ulid(), series=f"{dest}/Series B"),
    ):
        pass  # a shared holder lets another shared holder in
    with locks.hold(new_ulid(), work=new_ulid(), series=f"{dest}/Series A"):
        with pytest.raises(DetachRefusedError) as refused:
            _detach(d)
        assert refused.value.reason == "busy"


def test_the_shared_rows_of_a_destination_are_released(d: DetachEnv) -> None:
    locks = ResourceLocks(d.env.engine, clock=d.env.clock)
    with locks.hold(new_ulid(), work=new_ulid(), series=f"{d.env.destination_id}/Series A"):
        pass
    with d.env.engine.connect() as conn:
        assert (
            conn.execute(sa.select(sa.func.count()).select_from(coherence.resource_lock)).scalar()
            == 0
        )


# -- M1 : the digest is read again just before each erasure ----------------------------------------


def test_a_file_changed_between_the_look_and_the_unlink_stays(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    victim = sorted(d.env.files())[0]
    real = d.journal.mark_applying

    def edit_after(intent_id: str, **k: Any) -> None:
        real(intent_id, **k)
        if Path(victim).exists() and not getattr(edit_after, "done", False):
            edit_after.done = True  # type: ignore[attr-defined]
            Path(victim).write_bytes(b"the admin's edit, a moment before the unlink")

    monkeypatch.setattr(d.journal, "mark_applying", edit_after)
    report = _remove(d, RemovalMode.DELETE_FILES)
    assert Path(victim).read_bytes().startswith(b"the admin's edit")
    assert [line.code for line in report.left] == ["changed_since_export"]
    assert report.deleted == 2 and d.count(export.shelf_destination) == 1  # not forgotten


def test_a_line_without_a_hash_is_kept_and_reported_never_deleted(d: DetachEnv) -> None:
    record = export.export_record
    with d.env.engine.begin() as conn:
        first = conn.execute(sa.select(record).order_by(record.c.rel_path).limit(1)).one()
        conn.execute(
            sa.update(record)
            .where(record.c.export_item_id == first.export_item_id)
            .values(file_hash=None)
        )
    report = _remove(d, RemovalMode.DELETE_FILES)
    assert [(x.code, x.path) for x in report.left] == [("no_hash", first.rel_path)]
    assert (d.env.root / first.rel_path).exists() and report.deleted == 2
    assert d.count(export.shelf_destination) == 1 and not report.complete


# -- M2, M3 : a library gone, giving up past the point of no return --------------------------------


def test_a_library_gone_in_kavita_is_said_and_the_detachment_finishes(d: DetachEnv) -> None:
    _clean_bins(d)
    d.wired.fake.libraries.clear()
    report = _detach(d)
    assert report.complete and "library_gone" in _codes(report)
    assert "settings_not_restored" not in _codes(report)


def test_give_up_after_the_erasure_changes_nothing_and_the_same_command_finishes(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_bins(d)
    real = Fsops.erase_host_area

    def die(*a: Any, **k: Any) -> Any:
        real(*a, **k)
        raise Killed

    monkeypatch.setattr(Fsops, "erase_host_area", die)
    with pytest.raises(Killed):
        _detach(d)
    monkeypatch.undo()
    before = d.intent_ids(), [r.state for r in d.intents()]
    report = d.detacher.settle(d.env.target_id, resume=False, principal=ADMIN)
    assert report.past_no_return and report.cancelled == 0
    assert (
        d.intent_ids(),
        [r.state for r in d.intents()],
    ) == before  # nothing closed, nothing cancelled
    assert d.count(export.shelf_destination) == 1
    assert _detach(d).complete


def test_give_up_before_the_erasure_cancels_and_counts_what_it_cancelled(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_bins(d)
    real = d.journal.record

    def die(*a: Any, **k: Any) -> Any:
        real(*a, **k)
        raise Killed

    monkeypatch.setattr(d.journal, "record", die)
    with pytest.raises(Killed):
        _detach(d)
    monkeypatch.undo()
    report = d.detacher.settle(d.env.target_id, resume=False, principal=ADMIN)
    assert not report.past_no_return and report.cancelled == 1
    assert d.intents("pending", "applying") == [] and d.hidden()  # the folder is still there


# -- M4 : the erase log, the library's own root ----------------------------------------------------


def test_the_erase_log_goes_only_when_it_is_asked_for_by_name(d: DetachEnv) -> None:
    _clean_bins(d)
    journal = d.root / TOMBSTONE_JOURNAL
    journal.parent.mkdir(exist_ok=True)
    journal.write_bytes(b"append-only tombstones")
    preview = d.detacher.preview(d.env.target_id)
    assert "tombstone_journal_erased" in {w.code for w in preview.warnings}
    before = d.hidden(), d.count(export.shelf_destination), d.revoker.calls
    with pytest.raises(DetachRefusedError) as refused:
        _detach(d)
    assert refused.value.reason == "tombstone_journal_present"
    assert (d.hidden(), d.count(export.shelf_destination), d.revoker.calls) == before
    assert journal.exists()
    report = _detach(d, discard_tombstone_journal=True)
    assert report.complete and "tombstone_journal_erased" in _codes(report)  # the report says so
    assert not journal.exists()


def test_a_root_the_library_of_shelfhold_lives_on_is_left_alone(d: DetachEnv) -> None:
    _clean_bins(d)
    d.library_roots.add(d.env.lib.library_root)
    report = _detach(d)
    assert "root_shared" in _codes(report) and (d.root / ".shelfhold").exists()
    assert (d.root / SENTINEL_FILE).exists()


# -- M5 : the copy rows of another destination ----------------------------------------------------


def test_removing_a_destination_forgets_only_its_own_copy_rows(d: DetachEnv) -> None:
    add_second_shelf(d.env)  # a second destination of the same shelf's target, in `Kavita2`
    rows = _copies(d)
    assert len(rows) == 3
    table = replica_schema.replica
    moved = rows[0]
    with d.env.engine.begin() as conn:  # the same item, now living in the other destination
        conn.execute(
            sa.update(table)
            .where(table.c.id == moved.id)
            .values(rel_path="manga/Kavita2/Dr Stone/moved.cbz")
        )
    _remove(d, RemovalMode.KEEP_FILES)
    left = _copies(d)
    assert [r.id for r in left] == [moved.id]  # its own went, the other destination's stays


def test_detaching_forgets_only_the_copy_rows_under_the_destinations(d: DetachEnv) -> None:
    _clean_bins(d)
    table = replica_schema.replica
    outside = _copies(d)[0]
    with d.env.engine.begin() as conn:
        conn.execute(
            sa.update(table)
            .where(table.c.id == outside.id)
            .values(rel_path="manga/Elsewhere/x.cbz")
        )
    assert _detach(d).complete
    assert [r.id for r in _copies(d)] == [outside.id]


# -- the minors ------------------------------------------------------------------------------------


def test_a_library_type_changed_by_hand_is_told_and_not_forced(d: DetachEnv) -> None:
    _clean_bins(d)
    d.wired.fake.libraries[0].update(type=1, metadataProvider=2)  # a Comic one, adopted as Manga
    report = _detach(d)
    assert report.complete and "library_type_changed" in _codes(report)


def test_the_type_of_the_adoption_is_compared_whatever_its_case(d: DetachEnv) -> None:
    _clean_bins(d)
    assert "library_type_changed" not in _codes(_detach(d))  # `Manga` then, a manga library now


def test_every_destination_adopted_on_a_library_gives_its_own_patterns_back(d: DetachEnv) -> None:
    _clean_bins(d)
    _shelf, second = add_second_shelf(d.env)
    with d.env.engine.begin() as conn:
        conn.execute(
            sa.update(export.shelf_destination)
            .where(export.shelf_destination.c.id == second)
            .values(
                adopted=True,
                pre_adoption_settings={
                    "library_type": "Manga",
                    "exclude_patterns": ["*.tmp", "**/.shelfhold/**"],
                },
            )
        )
    assert _detach(d).complete
    # the library already had Shelfhold's own pattern before the second adoption: it stays
    assert d.kavita_patterns() == ["*.tmp", "**/.shelfhold/**"]


def test_the_sentinel_goes_last(d: DetachEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_bins(d)
    order: list[str] = []
    real = ConfinedRoot.unlink

    def spy(self: ConfinedRoot, rel: str) -> None:
        order.append(rel)
        real(self, rel)

    monkeypatch.setattr(ConfinedRoot, "unlink", spy)
    assert _detach(d).complete
    assert order[-1] == SENTINEL_FILE


def test_the_series_folders_a_removal_emptied_go_and_a_folder_with_something_else_stays(
    d: DetachEnv,
) -> None:
    extra = export_chapters(d.env, "Another Series", ["1"])
    del extra
    folders = sorted({Path(f).relative_to(d.env.root).parts[0] for f in d.env.files()})
    assert len(folders) == 2
    (d.env.root / folders[1] / "notes.txt").write_text("the admin's own file")
    report = _remove(d, RemovalMode.DELETE_FILES)
    assert report.complete
    assert not (d.env.root / folders[0]).exists()  # emptied: gone
    assert (d.env.root / folders[1] / "notes.txt").exists()  # something else in it: it stays
    assert d.env.root.exists()  # the destination's own folder is never removed
    assert d.intents("pending", "applying") == []
