"""Removing a destination (M3 L30, EF-EXP-031): keep the files and forget the manifest, or take
everything Shelfhold wrote away. A reserved action, typed, with the warning first; every deletion
goes through a journaled intention; an interruption (kill -9) finishes or gives up cleanly; and the
attacks: a child, a member, a key, a stale session, a path out of the root, a link."""

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.remove_destination import (
    RemovalMode,
    RemovalRefusedError,
    removal_phrase,
)
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import coherence, export
from tests.detach_env import ADMIN, OTHERS, DetachEnv, detach_env, fresh_admin
from tests.rename_env import export_chapters

pytestmark = pytest.mark.req("EF-EXP-031")


class Killed(BaseException):
    """What a `kill -9` looks like from inside the process: nothing after it runs."""


@pytest.fixture
def d() -> Iterator[DetachEnv]:
    with detach_env() as made:
        export_chapters(made.env, "Dr Stone", ["1", "2", "3"])
        yield made


def _adopt_one(d: DetachEnv) -> str:
    """A file the admin already had: adopted, never Shelfhold's, so never removed."""
    path = d.env.root / "Old Series" / "Old Series v01.cbz"
    path.parent.mkdir()
    path.write_bytes(b"the admin's own volume" * 100)
    with d.env.engine.begin() as conn:
        item = conn.execute(sa.select(export.export_record.c.export_item_id).limit(1)).scalar_one()
        conn.execute(
            sa.update(export.export_record)
            .where(export.export_record.c.export_item_id == item)
            .values(state="adopted", rel_path="Old Series/Old Series v01.cbz")
        )
    # the adopted line stands for the file above; the other chapters stay Shelfhold's
    return "Old Series/Old Series v01.cbz"


def _state(d: DetachEnv) -> tuple[Any, ...]:
    return (d.tree_digest(), d.count(export.export_record), d.count(export.shelf_destination))


@pytest.mark.req("EF-EXP-031")
def test_remove_destination_keep_or_delete_files_warns(d: DetachEnv) -> None:
    dest = d.env.destination_id
    untouched = _state(d)
    keep = d.remover.preview(dest, RemovalMode.KEEP_FILES)
    every = d.remover.preview(dest, RemovalMode.DELETE_FILES)
    assert {w.code for w in keep.warnings} == {"files_kept", "manifest_forgotten"}
    assert {w.code for w in every.warnings} >= {"files_deleted", "past_the_bin"}
    assert next(w for w in every.warnings if w.code == "files_deleted").count == 3
    assert keep.phrase == every.phrase == removal_phrase(dest)
    assert _state(d) == untouched  # a preview writes nothing
    assert d.intents() == []
    # keep the files: the tree is exactly as it was, the manifest and the destination are gone
    before = d.tree_digest()
    report = d.remover.apply(
        dest, RemovalMode.KEEP_FILES, principal=ADMIN, typed=removal_phrase(dest)
    )
    assert d.tree_digest() == before and report.deleted == 0 and report.forgotten == 3
    assert d.count(export.export_record) == 0 and d.count(export.shelf_destination) == 0
    assert d.intents("pending", "applying") == []


@pytest.mark.req("EF-EXP-031")
def test_remove_destination_delete_files_takes_only_what_shelfhold_wrote(d: DetachEnv) -> None:
    adopted = _adopt_one(d)
    dest = d.env.destination_id
    preview = d.remover.preview(dest, RemovalMode.DELETE_FILES)
    assert next(w for w in preview.warnings if w.code == "files_deleted").count == 2
    assert next(w for w in preview.warnings if w.code == "adopted_kept").count == 1
    report = d.remover.apply(
        dest, RemovalMode.DELETE_FILES, principal=fresh_admin(d.env), typed=removal_phrase(dest)
    )
    assert report.deleted == 2
    assert [str(p.relative_to(d.env.root)) for p in d.env.files()] == [adopted]  # the admin's own
    assert d.count(export.shelf_destination) == 0
    done = d.intents("done")
    assert len(done) == 2 and {i.op for i in done} == {"unlink"}  # one journaled deed per file


@pytest.mark.req("EF-EXP-031")
def test_every_removal_goes_through_a_journaled_intent(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No file leaves the disk before its intention is `applying` in the journal."""
    seen: list[str] = []

    def guard(real: Any) -> Any:
        def wrapper(self: Fsops, local: str, *a: Any, **k: Any) -> Any:
            with d.env.engine.connect() as conn:
                row = conn.execute(
                    sa.select(coherence.fs_intent.c.state).where(
                        coherence.fs_intent.c.src_path == local
                    )
                ).scalar()
            assert row == "applying", f"{local} was touched without an applying intent"
            seen.append(local)
            return real(self, local, *a, **k)

        return wrapper

    monkeypatch.setattr(Fsops, "purge_unlink", guard(Fsops.purge_unlink))
    dest = d.env.destination_id
    d.remover.apply(dest, RemovalMode.DELETE_FILES, principal=ADMIN, typed=removal_phrase(dest))
    assert len(seen) == 3


@pytest.mark.req("EF-EXP-031", "EF-USR-024")
@pytest.mark.parametrize("mode", list(RemovalMode))
@pytest.mark.parametrize("who", range(4), ids=["key", "stale_session", "member", "child"])
def test_remove_destination_refused_to_key_member_child_stale_session(
    d: DetachEnv, mode: RemovalMode, who: int
) -> None:
    dest = d.env.destination_id
    before = _state(d)
    with pytest.raises(RemovalRefusedError):
        d.remover.apply(dest, mode, principal=OTHERS[who], typed=removal_phrase(dest))
    assert _state(d) == before and d.intents() == []


@pytest.mark.req("EF-EXP-031")
@pytest.mark.parametrize("typed", [None, "", "yes", "remove", "remove other"])
def test_remove_destination_needs_the_typed_phrase(d: DetachEnv, typed: str | None) -> None:
    before = _state(d)
    with pytest.raises(RemovalRefusedError) as refused:
        d.remover.apply(
            d.env.destination_id, RemovalMode.DELETE_FILES, principal=ADMIN, typed=typed
        )
    assert refused.value.reason == "confirmation_required"
    assert _state(d) == before and d.intents() == []


@pytest.mark.req("EF-EXP-031")
def test_remove_destination_never_leaves_the_managed_root(d: DetachEnv) -> None:
    """A manifest line forged to point out of the root, and a managed file swapped for a link to
    a file elsewhere: neither the outside file nor the link's target is touched."""
    outside = d.env.lib.library / "outside.cbz"
    outside.write_bytes(b"not Shelfhold's")
    with d.env.engine.begin() as conn:
        records = list(conn.execute(sa.select(export.export_record)))
        conn.execute(
            sa.update(export.export_record)
            .where(export.export_record.c.export_item_id == records[0].export_item_id)
            .values(rel_path="../outside.cbz")
        )
    linked = d.env.root / str(records[1].rel_path)
    keep = d.env.lib.library / "linked-target.cbz"
    keep.write_bytes(b"the target of a planted link")
    linked.unlink()
    linked.symlink_to(keep)
    dest = d.env.destination_id
    report = d.remover.apply(
        dest, RemovalMode.DELETE_FILES, principal=ADMIN, typed=removal_phrase(dest)
    )
    assert outside.read_bytes() == b"not Shelfhold's"
    assert keep.read_bytes() == b"the target of a planted link"
    assert report.deleted == 1  # the one honest file
    assert {line.code for line in report.left} == {"outside_root", "not_a_regular_file"}
    assert d.count(export.shelf_destination) == 1  # not forgotten: something is left to look at
    assert d.intents("pending", "applying") == []


@pytest.mark.req("EF-EXP-031", "EF-EXP-041")
@pytest.mark.parametrize("stage", ["after_record", "after_applying", "after_unlink"])
def test_remove_destination_interrupted_resumes_to_the_end(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    dest = d.env.destination_id
    owner, name = {
        "after_record": (d.journal, "record"),
        "after_applying": (d.journal, "mark_applying"),
        "after_unlink": (Fsops, "purge_unlink"),
    }[stage]
    real = getattr(owner, name)

    def die(*a: Any, **k: Any) -> Any:
        result = real(*a, **k)
        raise Killed

    monkeypatch.setattr(owner, name, die)
    with pytest.raises(Killed):
        d.remover.apply(dest, RemovalMode.DELETE_FILES, principal=ADMIN, typed=removal_phrase(dest))
    monkeypatch.undo()
    assert d.intents("pending", "applying")  # the stop left its deed open
    # the same command, again, finishes: what was gone stays gone, the rest goes
    report = d.remover.apply(
        dest, RemovalMode.DELETE_FILES, principal=ADMIN, typed=removal_phrase(dest)
    )
    assert report.complete
    assert d.env.files() == [] and d.count(export.shelf_destination) == 0
    assert d.intents("pending", "applying") == []


@pytest.mark.req("EF-EXP-031", "EF-EXP-041")
def test_remove_destination_interrupted_can_be_given_up_cleanly(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = d.env.destination_id
    real = d.journal.mark_applying
    count = {"n": 0}

    def die(*a: Any, **k: Any) -> Any:
        count["n"] += 1
        if count["n"] == 2:
            raise Killed
        return real(*a, **k)

    monkeypatch.setattr(d.journal, "mark_applying", die)
    with pytest.raises(Killed):
        d.remover.apply(dest, RemovalMode.DELETE_FILES, principal=ADMIN, typed=removal_phrase(dest))
    monkeypatch.undo()
    report = d.remover.settle(dest, resume=False, principal=ADMIN)
    assert d.intents("pending", "applying") == []  # nothing left half-done
    assert report.cancelled >= 1
    assert d.count(export.shelf_destination) == 1  # the destination is still there, as it was
    # what the disk and the manifest say agree: the file that went is not recorded as present
    names = {str(p.relative_to(d.env.root)) for p in d.env.files()}
    with d.env.engine.connect() as conn:
        recorded = {
            str(r.rel_path)
            for r in conn.execute(sa.select(export.export_record))
            if r.state == "written"
        }
    assert recorded == names


@pytest.mark.req("EF-EXP-031")
def test_remove_destination_refused_while_another_intent_covers_its_files(d: DetachEnv) -> None:
    folder = f"{d.env.root}/Dr Stone"
    d.journal.record(op="rmdir", src=folder)  # somebody else's open deed over the series folder
    before = _state(d)
    dest = d.env.destination_id
    with pytest.raises(RemovalRefusedError) as refused:
        d.remover.apply(dest, RemovalMode.DELETE_FILES, principal=ADMIN, typed=removal_phrase(dest))
    assert refused.value.reason == "intent_open" and _state(d) == before
