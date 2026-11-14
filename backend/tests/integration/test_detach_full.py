"""The full detachment (M3 L30, EF-EXP-031): the hidden files go, the Kavita library gets its
pre-adoption settings back, the dedicated key is revoked, the tree stays as it is and works without
Shelfhold, and the report lists what was left. A reserved action, typed, never for a key, a member
or a child; an interruption resumes or gives up; the attacks on the disk."""

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.detach import DetachRefusedError, full_detach_phrase
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import coherence, export, platform
from tests.detach_env import ADMIN, OTHERS, PRE_ADOPTION, DetachEnv, detach_env, fresh_admin
from tests.rename_env import export_chapters

pytestmark = pytest.mark.req("EF-EXP-031")


class Killed(BaseException):
    """`kill -9`, seen from inside: nothing after it runs."""


@pytest.fixture
def d() -> Iterator[DetachEnv]:
    with detach_env() as made:
        export_chapters(made.env, "Dr Stone", ["1", "2"])
        yield made


def _run(d: DetachEnv, *, who: Any = None, **kw: Any) -> Any:
    target = d.env.target_id
    return d.detacher.apply(
        target, principal=who or ADMIN, typed=kw.pop("typed", full_detach_phrase(target)),
        client=d.wired.client, revoker=d.revoker, credential="the account password", **kw,
    )  # fmt: skip


def _clean_bins(d: DetachEnv) -> None:
    """What an instance that has exported everything holds in its reserved folder: nothing a
    detachment could destroy (no bin entry, no archive waiting for its export)."""
    for name in ("recycle", "received"):
        shutil.rmtree(d.root / ".shelfhold" / name, ignore_errors=True)
    shutil.rmtree(d.env.lib.manga / ".shelfhold", ignore_errors=True)  # the fixture's own inbox


def _before(d: DetachEnv) -> tuple[Any, ...]:
    return (
        d.hidden(), d.tree_digest(), d.kavita_patterns(), d.count(export.shelf_destination),
        d.intent_ids(),
    )  # fmt: skip


@pytest.mark.req("EF-EXP-031")
def test_detach_leaves_autonomous_kavita_tree(d: DetachEnv) -> None:
    _clean_bins(d)
    assert d.hidden()  # the reserved folder and the sentinel are there to begin with
    exported = d.tree_digest()
    report = _run(d)
    assert report.complete
    assert d.hidden() == []  # no `.shelfhold` anywhere in the tree: the folder, the sentinel
    assert d.tree_digest() == exported  # the very same files, inodes and sizes: Kavita keeps them
    assert d.count(export.export_record) == 0 and d.count(export.shelf_destination) == 0
    assert d.intents("pending", "applying") == []
    with d.env.engine.connect() as conn:  # the root is no longer written, nor expected stamped
        modes = set(conn.execute(sa.select(platform.physical_root.c.mode)))
    assert ("adopt_ro",) in modes


@pytest.mark.req("EF-EXP-031")
def test_detach_restores_pre_adoption_settings_and_revokes_key(d: DetachEnv) -> None:
    _clean_bins(d)
    order: list[str] = []
    d.revoker.seen_before = lambda: order.append(
        f"revoke:patterns={d.kavita_patterns()}:hidden={bool(d.hidden())}"
    )
    assert d.kavita_patterns() != PRE_ADOPTION["exclude_patterns"]
    report = _run(d)
    assert d.kavita_patterns() == PRE_ADOPTION["exclude_patterns"]  # `*.tmp`, as before
    assert report.settings and report.settings[0].exclude_patterns_restored
    assert d.wired.fake.libraries[0]["type"] == 0  # the type is still the manga one of before
    assert d.revoker.calls == [(d.env.target_id, "the account password")]
    assert report.key_revoked
    # the order is the safety: the hidden folder is gone, then the patterns, then the key
    assert order == ["revoke:patterns=['*.tmp']:hidden=False"]
    assert d.count(export.export_target) == 0  # nothing keeps the key or the target any more


@pytest.mark.req("EF-EXP-031")
def test_detach_keeps_the_patterns_an_admin_added_after_adoption(d: DetachEnv) -> None:
    _clean_bins(d)
    d.wired.fake.libraries[0]["excludePatterns"].append("*.bak")
    _run(d)
    assert d.kavita_patterns() == ["*.tmp", "*.bak"]  # only Shelfhold's own pattern goes


@pytest.mark.req("EF-EXP-031")
def test_detach_report_lists_leftovers(d: DetachEnv) -> None:
    _clean_bins(d)
    (d.env.lib.store / "some-unit.cbz").write_bytes(b"the store keeps its copies")
    report = _run(d)
    left = {line.code: line.count for line in report.leftovers}
    assert {"suwayomi_downloads", "kavita_accounts", "store"} <= set(left)
    assert left["store"] >= 1  # /store stays, and the report says how much
    assert left["kavita_accounts"] >= 1  # the dedicated account stays in Kavita
    assert (d.env.lib.store / "some-unit.cbz").exists()
    assert report.hidden_removed  # and what went is told too


@pytest.mark.req("EF-EXP-031", "EF-USR-024")
@pytest.mark.parametrize("who", range(4), ids=["key", "stale_session", "member", "child"])
def test_detach_refused_to_key_member_child_stale_session(d: DetachEnv, who: int) -> None:
    before = _before(d)
    with pytest.raises(DetachRefusedError):
        _run(d, who=OTHERS[who])
    assert _before(d) == before and d.revoker.calls == []


@pytest.mark.req("EF-EXP-031")
@pytest.mark.parametrize("typed", [None, "", "yes", "detach", "detach 01J00000000000000000000000"])
def test_detach_needs_the_typed_phrase(d: DetachEnv, typed: str | None) -> None:
    before = _before(d)
    with pytest.raises(DetachRefusedError) as refused:
        _run(d, typed=typed)
    assert refused.value.reason == "confirmation_required"
    assert _before(d) == before and d.revoker.calls == []


@pytest.mark.req("EF-EXP-031")
def test_detach_works_for_a_fresh_admin_session(d: DetachEnv) -> None:
    _clean_bins(d)
    assert _run(d, who=fresh_admin(d.env)).complete


@pytest.mark.req("EF-EXP-031")
def test_detach_preview_warns_and_writes_nothing(d: DetachEnv) -> None:
    before = _before(d)
    preview = d.detacher.preview(d.env.target_id)
    assert preview.phrase == full_detach_phrase(d.env.target_id)
    assert {"files_kept", "hidden_erased", "key_revoked"} <= {w.code for w in preview.warnings}
    assert _before(d) == before


@pytest.mark.req("EF-EXP-031")
def test_detach_refuses_to_destroy_what_the_bin_and_the_inbox_hold(d: DetachEnv) -> None:
    """The reserved folder is not only metadata: a bin entry is a deleted book still recoverable
    and a received archive is a book not yet exported. Neither goes without being asked for."""
    shutil.rmtree(d.env.lib.manga / ".shelfhold", ignore_errors=True)  # the fixture's own inbox
    (d.root / ".shelfhold" / "recycle" / "entry").mkdir(parents=True, exist_ok=True)
    (d.root / ".shelfhold" / "recycle" / "entry" / "Ch.9.cbz").write_bytes(b"a deleted book")
    before = _before(d)
    with pytest.raises(DetachRefusedError) as refused:
        _run(d)
    assert refused.value.reason == "hidden_data_present"
    assert _before(d) == before and d.revoker.calls == []
    assert (d.root / ".shelfhold" / "recycle" / "entry" / "Ch.9.cbz").exists()
    assert _run(d, discard_hidden_data=True).complete  # asked for, by name
    assert d.hidden() == []


@pytest.mark.req("EF-EXP-031")
def test_detach_never_follows_a_link_out_of_the_root(d: DetachEnv, tmp_path: Path) -> None:
    """`.shelfhold` swapped for a link to a folder somewhere else: the folder is not emptied."""
    _clean_bins(d)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "precious.txt").write_text("not Shelfhold's")
    shutil.rmtree(d.root / ".shelfhold")
    (d.root / ".shelfhold").symlink_to(elsewhere, target_is_directory=True)
    report = _run(d)
    assert (elsewhere / "precious.txt").read_text() == "not Shelfhold's"
    assert not report.complete and "hidden_not_erased" in {line.code for line in report.leftovers}
    assert d.revoker.calls == []  # the key is not revoked behind an unfinished erasure


@pytest.mark.req("EF-EXP-031")
def test_detach_skips_a_root_another_target_still_uses(d: DetachEnv) -> None:
    _clean_bins(d)
    with d.env.engine.begin() as conn:
        other = new_ulid()
        conn.execute(
            sa.insert(export.export_target).values(
                id=other, kind="folder", name="Another", path_map=[
                    {"local": str(d.env.lib.library), "remote": "/lib", "mode": "rw"}
                ],
            )
        )  # fmt: skip
    report = _run(d)
    assert (d.root / ".shelfhold").exists()  # the other target's folder still needs it
    assert "root_shared" in {line.code for line in report.leftovers}


@pytest.mark.req("EF-EXP-031")
def test_detach_without_adoption_only_removes_shelfhold_patterns() -> None:
    with detach_env(adopted=False) as d:
        d.wired.fake.libraries[0]["excludePatterns"] = ["*.tmp", "**/.shelfhold/**"]
        export_chapters(d.env, "Dr Stone", ["1"])
        _clean_bins(d)
        assert _run(d).complete
        assert d.kavita_patterns() == ["*.tmp"]


@pytest.mark.req("EF-EXP-031")
def test_detach_incomplete_when_the_key_cannot_be_revoked_and_finishes_on_rerun(
    d: DetachEnv,
) -> None:
    _clean_bins(d)
    d.revoker.answer = False
    first = _run(d)
    assert not first.complete and "key_not_revoked" in {line.code for line in first.leftovers}
    assert d.hidden() == [] and d.kavita_patterns() == ["*.tmp"]  # the rest was done
    assert d.count(export.shelf_destination) == 1  # the manifest waits for the key
    d.revoker.answer = True
    second = _run(d)
    assert second.complete and d.count(export.shelf_destination) == 0


@pytest.mark.req("EF-EXP-031", "EF-EXP-041")
@pytest.mark.parametrize(
    "stage", ["after_record", "after_applying", "after_erase", "before_revoke"]
)
def test_detach_interrupted_resumes_to_the_end(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    _clean_bins(d)
    exported = d.tree_digest()
    if stage == "before_revoke":
        d.revoker.raises = Killed()
    else:
        owner, name = {
            "after_record": (d.journal, "record"),
            "after_applying": (d.journal, "mark_applying"),
            "after_erase": (Fsops, "erase_host_area"),
        }[stage]
        real = getattr(owner, name)

        def die(*a: Any, **k: Any) -> Any:
            real(*a, **k)
            raise Killed

        monkeypatch.setattr(owner, name, die)
    with pytest.raises(Killed):
        _run(d)
    monkeypatch.undo()
    d.revoker.raises = None
    assert d.tree_digest() == exported  # the interruption cost no exported file
    report = _run(d)  # the same command finishes the job
    assert report.complete and d.hidden() == [] and d.kavita_patterns() == ["*.tmp"]
    assert d.revoker.calls == [(d.env.target_id, "the account password")]  # revoked exactly once
    assert d.tree_digest() == exported
    assert d.intents("pending", "applying") == []


@pytest.mark.req("EF-EXP-031", "EF-EXP-041")
def test_detach_interrupted_can_be_given_up_cleanly(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_bins(d)
    real = d.journal.mark_applying

    def die(*a: Any, **k: Any) -> Any:
        real(*a, **k)
        raise Killed

    monkeypatch.setattr(d.journal, "mark_applying", die)
    with pytest.raises(Killed):
        _run(d)
    monkeypatch.undo()
    assert d.intents("applying")
    report = d.detacher.settle(d.env.target_id, resume=False, principal=ADMIN)
    assert report.cancelled >= 1 and d.intents("pending", "applying") == []
    assert d.revoker.calls == [] and d.count(export.shelf_destination) == 1
    assert d.kavita_patterns() != PRE_ADOPTION["exclude_patterns"]  # nothing was half-restored


@pytest.mark.req("EF-EXP-031")
def test_detach_every_erasure_goes_through_an_applying_intent(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_bins(d)
    seen: list[str] = []
    real = Fsops.erase_host_area

    def guard(self: Fsops, root_path: str) -> Any:
        with d.env.engine.connect() as conn:
            states = set(
                conn.execute(
                    sa.select(coherence.fs_intent.c.state).where(
                        coherence.fs_intent.c.src_path.like(f"{root_path}%")
                    )
                ).scalars()
            )
        assert states == {"applying"}, "the hidden folder was erased without its intention"
        seen.append(root_path)
        return real(self, root_path)

    monkeypatch.setattr(Fsops, "erase_host_area", guard)
    _run(d)
    assert seen == [str(d.root)]


@pytest.mark.req("EF-EXP-031")
def test_detach_refused_while_an_intent_covers_the_root(d: DetachEnv) -> None:
    _clean_bins(d)
    d.journal.record(op="rmdir", src=f"{d.root}/manga/Kavita/Dr Stone")
    before = _before(d)
    with pytest.raises(DetachRefusedError) as refused:
        _run(d)
    assert refused.value.reason == "intent_open" and _before(d) == before
