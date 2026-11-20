"""EF-EXP-001, EF-EXP-058, EF-SRC-036: the executor of destination plans (M4 L04, intouchable).

A destination describes, the host executes. The plan a plugin hands over is never run as it
comes: the executor works on the copy `parse_destination_plan` returns and checks it again right
before each operation. Real files, a real database, the real intents; the only fakes are the
server's answer about itself and the bytes the host owns.

Attacks: a plan altered between two operations, a content reference planned for another call, for
another binding of the same server and for another server, a server that was reinstalled, an
indexer that asks to write, a tree folder behind a link. The executor is served to no profile (it
runs in the export job, with no route): a child, a member or an API key never reaches it, so the
attacks of the profiles are the ones of the routes that will call it (L06a).
"""

import ast
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from shelfhold_sdk.api_call import ApiCall, ContentBody, RawEncoding
from shelfhold_sdk.fsops import ContentRef, FsOpPlan, PlannedContent

from shelfhold.export.application import plan_executor
from shelfhold.export.application.plan_executor import PlanBinding, PlanRefusedError
from shelfhold.export.application.target_state import (
    DestinationPausedError,
    ensure_active,
    load_fingerprint,
    load_health,
    save_fingerprint,
)
from shelfhold.export.domain.bindings import TreeOverlapError
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import coherence
from tests.export_env import export_env
from tests.plan_executor_env import (
    BOOK_A,
    BOOK_B,
    BYTES,
    FINGERPRINT,
    ExecEnv,
    make_exec_env,
    second_binding,
    sha,
)

PLAN: dict[str, Any] = {
    "ops": [
        {"op": "mkdir", "rel_path": "Series A"},
        {
            "op": "write",
            "rel_path": "Series A/Volume 1.cbz",
            "content_ref": {"kind": "export_item", "ref": BOOK_A},
        },
        {
            "op": "write",
            "rel_path": "Series A/Volume 2.cbz",
            "content_ref": {"kind": "export_item", "ref": BOOK_B},
        },
    ]
}


@pytest.fixture
def x() -> Iterator[ExecEnv]:
    with export_env() as env:
        ex = make_exec_env(env)
        with write_transaction(env.engine) as connection:
            save_fingerprint(connection, env.target_id, FINGERPRINT, "0.9.0", env.clock.now())
        yield ex


def tree_files(x: ExecEnv) -> list[str]:
    return sorted(str(p.relative_to(x.root)) for p in x.root.rglob("*") if p.is_file())


def intents(x: ExecEnv, state: str | None = None) -> list[sa.Row[Any]]:
    with x.env.engine.connect() as connection:
        query = sa.select(coherence.fs_intent)
        if state:
            query = query.where(coherence.fs_intent.c.state == state)
        return list(connection.execute(query))


# ---------------------------------------------------------------------------- the check


@pytest.mark.req("EF-EXP-001", "EF-EXP-058")
def test_executor_checks_destination_plan_before_each_op(
    x: ExecEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every operation runs after a check of the copy with what the host planned: never the
    static check without `planned`, and a check right before the effect on the disk."""
    events: list[str] = []
    real = plan_executor.check_destination_plan_before_run

    def spy(plan: FsOpPlan, *, planned: PlannedContent) -> None:
        events.append("check")
        assert type(plan) is FsOpPlan and planned == x.ledger.planned(
            "c1", x.binding.destination_id
        )
        real(plan, planned=planned)

    monkeypatch.setattr(plan_executor, "check_destination_plan_before_run", spy)
    monkeypatch.setattr(
        plan_executor,
        "check_destination_plan",
        lambda *a, **k: pytest.fail(
            "the static check has no `planned`: the executor never uses it"
        ),
        raising=False,
    )
    fsops = x.env.lib.fsops
    for name in ("mkdir", "write_atomic"):
        real_effect = getattr(fsops, name)

        def effect(
            *args: Any, _real: Callable[..., Any] = real_effect, _n: str = name, **kw: Any
        ) -> Any:
            events.append(f"disk:{_n}")
            return _real(*args, **kw)

        monkeypatch.setattr(fsops, name, effect)
    x.plan_for("c1", BOOK_A, BOOK_B)

    report = x.executor.execute(x.binding, "c1", PLAN)

    assert report.executed == 3
    assert events.count("check") >= 3
    for position, event in enumerate(events):
        if event.startswith("disk:"):
            assert events[position - 1] == "check", events  # nothing touches the disk unchecked
    assert tree_files(x) == ["Series A/Volume 1.cbz", "Series A/Volume 2.cbz"]


@pytest.mark.req("EF-EXP-001")
def test_plan_altered_between_two_operations_is_refused(
    x: ExecEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host's copy is altered after the first operation (a property, an unsafe write): the
    second operation is refused, nothing of it reaches the disk."""
    copies: list[FsOpPlan] = []
    real_parse = plan_executor.parse_destination_plan

    def keep(raw: object) -> FsOpPlan:
        copies.append(real_parse(raw))
        return copies[-1]

    monkeypatch.setattr(plan_executor, "parse_destination_plan", keep)
    fsops, real_mkdir = x.env.lib.fsops, x.env.lib.fsops.mkdir

    def mkdir_then_alter(*args: Any, **kwargs: Any) -> None:
        real_mkdir(*args, **kwargs)
        object.__setattr__(copies[0].ops[1], "rel_path", ".shelfhold/evil")  # the plugin's reach

    monkeypatch.setattr(fsops, "mkdir", mkdir_then_alter)
    x.plan_for("c1", BOOK_A, BOOK_B)

    with pytest.raises(PlanRefusedError) as refused:
        x.executor.execute(x.binding, "c1", PLAN)

    assert refused.value.code == "export.plan_refused"
    assert tree_files(x) == []
    assert not (x.root / ".shelfhold").exists()
    assert intents(x, "pending") == [] and intents(x, "applying") == []


@pytest.mark.req("EF-EXP-001")
def test_plan_altered_after_the_handover_is_never_executed(
    x: ExecEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plugin keeps a reference to the object it handed over and edits it while the host runs:
    the host runs its own copy, as it was validated."""
    plan = FsOpPlan.model_validate(PLAN)
    fsops, real_mkdir = x.env.lib.fsops, x.env.lib.fsops.mkdir

    def mkdir_then_alter(*args: Any, **kwargs: Any) -> None:
        real_mkdir(*args, **kwargs)
        object.__setattr__(plan.ops[1], "rel_path", "Elsewhere/stolen.cbz")
        object.__setattr__(plan, "ops", plan.ops[:1])

    monkeypatch.setattr(fsops, "mkdir", mkdir_then_alter)
    x.plan_for("c1", BOOK_A, BOOK_B)

    x.executor.execute(x.binding, "c1", plan)

    assert tree_files(x) == ["Series A/Volume 1.cbz", "Series A/Volume 2.cbz"]


@pytest.mark.req("EF-EXP-001")
@pytest.mark.parametrize(
    "bad",
    [
        {"op": "unlink", "rel_path": "Series A/old.cbz"},
        {"op": "mkdir", "rel_path": ".shelfhold/x"},
        {"op": "mkdir", "rel_path": "../escape"},
        {"op": "mkdir", "rel_path": "SHELFH~1"},
    ],
)
def test_a_plan_the_sdk_refuses_never_starts(x: ExecEnv, bad: dict[str, str]) -> None:
    x.plan_for("c1", BOOK_A)
    with pytest.raises(PlanRefusedError):
        x.executor.execute(x.binding, "c1", {"ops": [PLAN["ops"][0], bad]})
    assert tree_files(x) == [] and not (x.root / "Series A").exists()


@pytest.mark.req("EF-EXP-001")
def test_executor_runs_rename_recycle_and_touch_under_intents(x: ExecEnv) -> None:
    x.plan_for("c1", BOOK_A)
    x.executor.execute(x.binding, "c1", {"ops": PLAN["ops"][:2]})
    x.plan_for("c2")
    x.executor.execute(
        x.binding,
        "c2",
        {
            "ops": [
                {
                    "op": "rename",
                    "rel_path": "Series A/Volume 1.cbz",
                    "dst_rel_path": "Series A/V1.cbz",
                },
                {
                    "op": "touch_mtime",
                    "rel_path": "Series A/V1.cbz",
                    "mtime": "2020-01-02T03:04:05+00:00",
                },
            ]
        },
    )
    kept = x.root / "Series A" / "V1.cbz"
    assert kept.read_bytes() == BYTES[BOOK_A] and int(kept.stat().st_mtime) == 1577934245
    x.plan_for("c3")
    x.executor.execute(x.binding, "c3", {"ops": [{"op": "recycle", "rel_path": "Series A/V1.cbz"}]})
    assert not kept.exists() and list((x.root.parent.parent / ".shelfhold").rglob("V1.cbz"))
    assert {i.op for i in intents(x, "done")} >= {"write", "rename", "recycle"}
    assert intents(x, "pending") == [] and intents(x, "applying") == []


@pytest.mark.req("EF-EXP-001")
def test_write_never_overwrites_a_file_the_host_did_not_write(x: ExecEnv) -> None:
    (x.root / "Series A").mkdir()
    (x.root / "Series A" / "Volume 1.cbz").write_bytes(b"somebody else's")
    x.plan_for("c1", BOOK_A, BOOK_B)
    with pytest.raises(PlanRefusedError):
        x.executor.execute(x.binding, "c1", PLAN)
    assert (x.root / "Series A" / "Volume 1.cbz").read_bytes() == b"somebody else's"


@pytest.mark.req("EF-EXP-001")
def test_write_inline_that_cites_an_export_item_follows_the_same_rule(x: ExecEnv) -> None:
    x.plan_for("c1", BOOK_A)
    plan = {
        "ops": [
            {
                "op": "write_inline",
                "rel_path": "S/inline.cbz",
                "content_ref": {"kind": "export_item", "ref": BOOK_B},
            }
        ]
    }
    with pytest.raises(PlanRefusedError):
        x.executor.execute(x.binding, "c1", plan)
    assert tree_files(x) == []


# ---------------------------------------------------------------------- planned references


@pytest.mark.req("EF-EXP-001")
def test_fsop_write_ref_must_be_planned_for_binding(x: ExecEnv) -> None:
    x.plan_for("c1", BOOK_A)  # BOOK_B is not planned for this call and this binding
    with pytest.raises(PlanRefusedError):
        x.executor.execute(x.binding, "c1", PLAN)
    assert tree_files(x) == []
    assert x.contents.reads == []  # the bytes of an unplanned item are never even read
    with pytest.raises(PlanRefusedError):  # a call nobody planned
        x.executor.execute(x.binding, "never-planned", {"ops": PLAN["ops"][:2]})
    assert tree_files(x) == []


@pytest.mark.req("EF-EXP-001")
@pytest.mark.parametrize(
    "new_target", [False, True], ids=["between-bindings", "between-destinations"]
)
def test_write_ref_planned_for_another_binding_is_refused(x: ExecEnv, new_target: bool) -> None:
    """IDOR: the item is planned for the other binding (same server, or another server), for the
    very same call. Citing it from this one is refused, in both directions."""
    other = second_binding(x, new_target=new_target)
    x.plan_for("c1", BOOK_A, binding=x.binding)
    x.plan_for("c1", BOOK_B, binding=other)

    with pytest.raises(PlanRefusedError):
        x.executor.execute(x.binding, "c1", {"ops": PLAN["ops"][:3]})  # cites BOOK_B too
    with pytest.raises(PlanRefusedError):
        x.executor.execute(
            other,
            "c1",
            {"ops": [PLAN["ops"][0], PLAN["ops"][1]]},  # cites BOOK_A, planned for the first
        )
    assert tree_files(x) == []
    assert not (x.env.lib.manga / "Other" / "Series A").exists()
    # each binding still writes what is its own
    x.executor.execute(x.binding, "c1", {"ops": PLAN["ops"][:2]})
    assert tree_files(x) == ["Series A/Volume 1.cbz"]


@pytest.mark.req("EF-EXP-001")
def test_write_ref_is_planned_for_one_call_only(x: ExecEnv) -> None:
    x.plan_for("c1", BOOK_A)
    with pytest.raises(PlanRefusedError):
        x.executor.execute(x.binding, "c2", {"ops": PLAN["ops"][:2]})
    x.executor.execute(x.binding, "c1", {"ops": PLAN["ops"][:2]})
    x.ledger.release("c1")
    with pytest.raises(PlanRefusedError):  # a released call plans nothing any more
        x.executor.execute(x.binding, "c1", {"ops": [{"op": "mkdir", "rel_path": "B"}]})


@pytest.mark.req("EF-EXP-001", "EF-EXP-058")
def test_planned_egress_is_the_one_the_instance_attested_and_a_watermarked_file_stays(
    x: ExecEnv,
) -> None:
    x.plan_for("c1", BOOK_A, BOOK_B, egress="externe", watermarked=frozenset({BOOK_B}))
    with pytest.raises(PlanRefusedError):
        x.executor.execute(x.binding, "c1", PLAN)
    assert tree_files(x) == []
    x.plan_for("c2", BOOK_A, BOOK_B, egress="foyer", watermarked=frozenset({BOOK_B}))
    x.executor.execute(x.binding, "c2", PLAN)
    assert len(tree_files(x)) == 2


@pytest.mark.req("EF-EXP-001", "EF-EXP-058")
def test_api_call_is_parsed_then_checked_right_before_each_send(x: ExecEnv) -> None:
    sent: list[str] = []
    x.plan_for("c1", BOOK_A)
    ok = ApiCall(
        method="POST",
        path="/api/upload",
        auth="target_admin",
        body=ContentBody(
            content_ref=ContentRef(kind="export_item", ref=BOOK_A), encoding=RawEncoding()
        ),
    )
    other_item = ok.model_copy(
        update={"body": ContentBody(content_ref=ContentRef(kind="export_item", ref=BOOK_B))}
    )
    with pytest.raises(PlanRefusedError):
        x.executor.run_calls(x.binding, "c1", [ok, other_item], send=lambda c: sent.append(c.path))
    assert sent == ["/api/upload"]  # the first went, the second was refused before it left
    purge = ApiCall(method="DELETE", path="/api/series/1", auth="target_admin")
    with pytest.raises(PlanRefusedError):  # Kavita declares no `can_purge`
        x.executor.run_calls(x.binding, "c1", [purge], send=lambda c: sent.append(c.path))
    assert sent == ["/api/upload"]


@pytest.mark.req("EF-EXP-001")
def test_no_dump_of_an_api_call_reaches_a_log_line() -> None:
    """#275 m3: the body of a call may hold a secret. The executor logs nothing, and the only
    dump it makes is the one of a file operation, which holds no secret."""
    tree = ast.parse(Path(plan_executor.__file__).read_text(encoding="utf-8"))
    dumps = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr.startswith("model_dump")
    ]
    assert dumps and all(isinstance(d.value, ast.Name) and d.value.id == "op" for d in dumps)
    assert not any(
        isinstance(node, ast.Call) and "log" in ast.unparse(node.func).lower()
        for node in ast.walk(tree)
    )


# ------------------------------------------------------------------ instance fingerprint


@pytest.mark.req("EF-SRC-036")
def test_destination_instance_fingerprint_change_pauses(x: ExecEnv) -> None:
    """The server answers with another identity (reinstalled): the binding is paused with a typed
    code, nothing is written, and the pause holds until the admin reconnects."""
    x.plan_for("c1", BOOK_A, BOOK_B)
    x.probe.current = "sha256:" + "b" * 64

    with pytest.raises(DestinationPausedError) as paused:
        x.executor.execute(x.binding, "c1", PLAN)

    assert paused.value.code == "target.instance_changed"
    assert tree_files(x) == [] and intents(x) == []
    with x.env.engine.connect() as connection:
        assert load_health(connection, x.binding.target_id).instance_changed is True
        assert load_fingerprint(connection, x.binding.target_id) == FINGERPRINT  # not overwritten
        with pytest.raises(DestinationPausedError):
            ensure_active(connection, x.binding.destination_id)
    x.probe.current = FINGERPRINT  # the server is back: the pause still waits for a human
    with pytest.raises(DestinationPausedError):
        x.executor.execute(x.binding, "c1", PLAN)
    assert tree_files(x) == []


@pytest.mark.req("EF-SRC-036")
def test_destination_that_stops_answering_for_its_identity_pauses_too(x: ExecEnv) -> None:
    x.plan_for("c1", BOOK_A, BOOK_B)
    x.probe.current = None  # an unreadable identifier, once one was known, is a change
    with pytest.raises(DestinationPausedError):
        x.executor.execute(x.binding, "c1", PLAN)
    assert tree_files(x) == []


@pytest.mark.req("EF-SRC-036")
def test_first_sight_of_an_instance_is_recorded_not_a_change(x: ExecEnv) -> None:
    with write_transaction(x.env.engine) as connection:
        connection.execute(sa.text("DELETE FROM kavita_instance"))
    x.plan_for("c1", BOOK_A, BOOK_B)
    x.executor.execute(x.binding, "c1", PLAN)
    with x.env.engine.connect() as connection:
        assert load_fingerprint(connection, x.binding.target_id) == FINGERPRINT
    assert len(tree_files(x)) == 2


@pytest.mark.req("EF-SRC-036")
def test_api_calls_wait_for_the_same_pause(x: ExecEnv) -> None:
    x.plan_for("c1")
    x.probe.current = "sha256:" + "c" * 64
    sent: list[str] = []
    with pytest.raises(DestinationPausedError):
        x.executor.run_calls(
            x.binding,
            "c1",
            [ApiCall(method="GET", path="/api/x", auth="target_admin")],
            send=lambda c: sent.append(c.path),
        )
    assert sent == []


# ----------------------------------------------------------------------- trees, indexers


@pytest.mark.req("EF-EXP-058")
def test_an_indexer_never_writes_into_the_tree(x: ExecEnv) -> None:
    """ADR-0052 T3: only the primary binding of a tree renders and writes. The role is the one
    the database holds, never the one a caller says: a secondary indexer that returns a plan is
    refused, whatever the plan."""
    with write_transaction(x.env.engine) as connection:
        connection.execute(sa.text("UPDATE tree_indexer SET role = 'secondary'"))
    x.plan_for("c1", BOOK_A, BOOK_B)
    with pytest.raises(PlanRefusedError):
        x.executor.execute(x.binding, "c1", PLAN)
    assert tree_files(x) == [] and intents(x) == []


@pytest.mark.req("EF-EXP-058")
def test_a_binding_with_no_tree_is_refused(x: ExecEnv) -> None:
    with write_transaction(x.env.engine) as connection:
        connection.execute(sa.text("DELETE FROM tree_indexer"))
    x.plan_for("c1", BOOK_A, BOOK_B)
    with pytest.raises(PlanRefusedError):
        x.executor.execute(x.binding, "c1", PLAN)
    assert tree_files(x) == []


@pytest.mark.req("EF-EXP-058")
def test_tree_that_overlaps_another_is_refused_at_execution(x: ExecEnv) -> None:
    """Defence in depth: the creation of a binding refuses an overlap, and the executor looks
    again at the trees the database holds before it writes."""
    other = second_binding(x, new_target=False)
    with write_transaction(x.env.engine) as connection:
        connection.execute(
            sa.text(
                "UPDATE tree SET root_subpath = 'manga/Kavita/Inner' "
                "WHERE root_subpath = 'manga/Other'"
            )
        )
    x.plan_for("c1", BOOK_A, BOOK_B)
    with pytest.raises(TreeOverlapError):
        x.executor.execute(x.binding, "c1", PLAN)
    with pytest.raises(TreeOverlapError):  # and from the inner one
        x.executor.execute(other, "c1", {"ops": []})
    assert tree_files(x) == []


@pytest.mark.req("EF-EXP-058")
def test_tree_folder_behind_a_symbolic_link_is_refused(x: ExecEnv, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    x.root.rmdir()
    x.root.symlink_to(elsewhere, target_is_directory=True)
    x.plan_for("c1", BOOK_A, BOOK_B)
    with pytest.raises(PlanRefusedError):
        x.executor.execute(x.binding, "c1", PLAN)
    assert list(elsewhere.iterdir()) == []


@pytest.mark.req("EF-EXP-001")
def test_a_binding_that_is_not_a_tree_never_gets_a_file_plan(x: ExecEnv) -> None:
    pulled = PlanBinding(
        destination_id=x.binding.destination_id,
        target_id=x.binding.target_id,
        capabilities=x.binding.capabilities.model_copy(update={"transport": "pull"}),
    )
    x.plan_for("c1", BOOK_A, BOOK_B)
    with pytest.raises(PlanRefusedError):
        x.executor.execute(pulled, "c1", PLAN)
    assert tree_files(x) == []


# ------------------------------------------------------------------------------ resume


@pytest.mark.req("EF-EXP-041", "EF-EXP-001")
def test_a_second_run_of_the_same_call_skips_what_is_done(x: ExecEnv) -> None:
    x.plan_for("c1", BOOK_A, BOOK_B)
    first = x.executor.execute(x.binding, "c1", PLAN)
    again = x.executor.execute(x.binding, "c1", PLAN)
    assert (first.executed, again.executed, again.skipped) == (3, 0, 3)
    assert sorted(p.read_bytes() for p in x.root.rglob("*.cbz")) == sorted(BYTES.values())
    done = intents(x, "done")
    assert [i.op for i in done].count("write") == 2  # no second intent, no duplicate


@pytest.mark.req("EF-EXP-041")
def test_an_open_intent_of_the_call_is_settled_before_the_call_runs_again(x: ExecEnv) -> None:
    x.plan_for("c1", BOOK_A, BOOK_B)
    x.executor.execute(x.binding, "c1", {"ops": PLAN["ops"][:2]})
    row = intents(x, "done")[0]
    with write_transaction(x.env.engine) as connection:
        connection.execute(
            sa.update(coherence.fs_intent)
            .where(coherence.fs_intent.c.id == row.id)
            .values(state="applying")
        )
    with pytest.raises(PlanRefusedError):  # the boot settles it first; nothing is replayed blind
        x.executor.execute(x.binding, "c1", PLAN)
    x.journal.resolve_open()
    report = x.executor.execute(x.binding, "c1", PLAN)
    assert len(tree_files(x)) == 2 and report.executed >= 1


# --------------------------------------------------------- #299: the keys, the sidecars, the time


@pytest.mark.req("EF-EXP-041", "EF-EXP-001")
def test_a_plan_made_again_keeps_the_keys_of_what_it_still_asks_for(x: ExecEnv) -> None:
    """The resume is by the key of each operation (its type, target, reference), never by its
    rank: the same operation in another place of a new plan is still done, a new one is run, and
    the report counts as skipped only what was done."""
    x.plan_for("c1", BOOK_A, BOOK_B)
    first = x.executor.execute(x.binding, "c1", {"ops": PLAN["ops"][:2]})  # mkdir, volume 1
    regenerated = {"ops": [PLAN["ops"][2], PLAN["ops"][1], PLAN["ops"][0]]}  # another order
    second = x.executor.execute(x.binding, "c1", regenerated)
    assert (first.executed, first.skipped) == (2, 0)
    assert (second.executed, second.skipped) == (1, 2)  # only volume 2 was missing
    assert len(tree_files(x)) == 2


@pytest.mark.req("EF-EXP-001")
def test_a_key_is_the_content_of_the_operation_not_its_place(x: ExecEnv) -> None:
    x.plan_for("c1", BOOK_A, BOOK_B)
    x.executor.execute(x.binding, "c1", {"ops": [PLAN["ops"][1]]})
    # the same target with another content reference is another operation: refused (it exists)
    again = {"ops": [{**PLAN["ops"][1], "content_ref": {"kind": "export_item", "ref": BOOK_B}}]}
    with pytest.raises(PlanRefusedError):
        x.executor.execute(x.binding, "c1", again)
    assert (x.root / "Series A" / "Volume 1.cbz").read_bytes() == BYTES[BOOK_A]


@pytest.mark.req("EF-EXP-058")
def test_a_sidecar_the_host_planned_is_written_and_series_json_never(x: ExecEnv) -> None:
    x.contents.data["side-1"] = b"<ComicInfo/>"
    x.contents.data["side-2"] = b"{}"
    x.ledger.plan(
        "c1",
        x.binding.destination_id,
        PlannedContent(planned_refs=frozenset(), egress="foyer"),
        sidecars=frozenset({"side-1", "side-2"}),
    )

    def inline(rel: str, ref: str) -> dict[str, Any]:
        return {
            "op": "write_inline",
            "rel_path": rel,
            "content_ref": {"kind": "sidecar", "ref": ref},
        }

    x.executor.execute(x.binding, "c1", {"ops": [inline("S/ComicInfo.xml", "side-1")]})
    assert (x.root / "S" / "ComicInfo.xml").read_bytes() == b"<ComicInfo/>"
    for name in ("series.json", "Series.JSON", "S/SERIES.json "):  # the case and the dot are lax
        with pytest.raises(PlanRefusedError):
            x.executor.execute(x.binding, "c1", {"ops": [inline(name, "side-2")]})
    renamed = {
        "ops": [{"op": "rename", "rel_path": "S/ComicInfo.xml", "dst_rel_path": "S/series.json"}]
    }
    with pytest.raises(PlanRefusedError):  # a rename onto the forbidden name is the same thing
        x.executor.execute(x.binding, "c1", renamed)
    assert not list(x.root.rglob("series.json"))


@pytest.mark.req("EF-EXP-001")
def test_touch_mtime_sets_the_time_in_place_and_journals_it(x: ExecEnv) -> None:
    x.plan_for("c1", BOOK_A)
    x.executor.execute(x.binding, "c1", {"ops": [PLAN["ops"][1]]})
    served = x.root / "Series A" / "Volume 1.cbz"
    before = served.stat()
    x.plan_for("c2")
    touch = {
        "op": "touch_mtime",
        "rel_path": "Series A/Volume 1.cbz",
        "mtime": "2020-01-02T03:04:05+00:00",
    }
    x.executor.execute(x.binding, "c2", {"ops": [touch]})
    after = served.stat()
    assert (after.st_ino, after.st_size) == (before.st_ino, before.st_size)
    assert int(after.st_mtime) == 1577934245
    assert served.read_bytes() == BYTES[BOOK_A]
    row = [i for i in intents(x, "done") if i.op == "utime"]
    assert len(row) == 1 and row[0].expected_sha256 == sha(BYTES[BOOK_A])


@pytest.mark.req("EF-EXP-001")
def test_touch_mtime_of_something_that_is_not_a_file_is_refused(x: ExecEnv) -> None:
    x.plan_for("c1")
    x.executor.execute(x.binding, "c1", {"ops": [PLAN["ops"][0]]})  # the folder
    folder = {"op": "touch_mtime", "rel_path": "Series A", "mtime": "2020-01-02T03:04:05+00:00"}
    missing = {**folder, "rel_path": "Series A/none.cbz"}
    for bad in (folder, missing):
        with pytest.raises(PlanRefusedError):
            x.executor.execute(x.binding, "c1", {"ops": [bad]})
    assert intents(x) == []


@pytest.mark.req("EF-SRC-036")
def test_the_identity_is_read_again_every_n_operations(x: ExecEnv) -> None:
    """`recheck_every` bounds the reads: with 2, the swap that follows the first write is seen
    before the third operation, not the second."""
    from shelfhold.export.application.plan_executor import PlanExecutor

    slow = PlanExecutor(
        x.env.engine, x.env.lib.fsops, x.journal, ledger=x.ledger, contents=x.contents,
        probe=x.probe, clock=x.env.clock, recheck_every=2,
    )  # fmt: skip
    real = x.contents.read

    def read_then_swap(destination_id: str, ref: Any) -> bytes:
        data = real(destination_id, ref)
        x.probe.current = "sha256:" + "b" * 64
        return data

    x.contents.read = read_then_swap  # type: ignore[method-assign,assignment]
    x.plan_for("c1", BOOK_A, BOOK_B)
    with pytest.raises(DestinationPausedError):
        slow.execute(x.binding, "c1", PLAN)
    assert tree_files(x) == ["Series A/Volume 1.cbz"]  # the second write was one more than allowed


@pytest.mark.req("EF-EXP-058")
def test_the_target_of_a_binding_is_read_from_the_database(x: ExecEnv) -> None:
    other = second_binding(x, new_target=True)
    crossed = PlanBinding(
        destination_id=x.binding.destination_id,
        target_id=other.target_id,
        capabilities=x.binding.capabilities,
    )
    x.plan_for("c1", BOOK_A, BOOK_B)
    with pytest.raises(PlanRefusedError):
        x.executor.execute(crossed, "c1", PLAN)
    with pytest.raises(PlanRefusedError):  # nor a transport the binding does not have
        x.executor.execute(
            PlanBinding(
                destination_id=x.binding.destination_id,
                target_id=x.binding.target_id,
                capabilities=x.binding.capabilities.model_copy(update={"transport": "dropbox"}),
            ),
            "c1",
            PLAN,
        )
    assert tree_files(x) == []
