"""Counter-review of #239 (M3 L29) after the answers: proofs of concept, red until answered.

See `docs/decisions/M3.md`, "L29 : contradicteur de la #239", "Contre-relecture"."""

import signal
from collections.abc import Iterator
from types import FrameType

import pytest

from shelfhold.export.application.rename_plan import (
    ItemMove,
    RenameConfirmationRequiredError,
    RenameRefusedError,
)
from shelfhold.infrastructure.ids import new_ulid
from tests.export_env import ExportEnv, export_env
from tests.rename_env import FakeReaders, export_chapters, item_ids_of, record_paths, tree


@pytest.fixture
def shelf() -> Iterator[ExportEnv]:
    with export_env() as made:
        yield made


class _Hung(Exception):
    pass


def _alarm(_signum: int, _frame: FrameType | None) -> None:
    raise _Hung


@pytest.mark.req("EF-EXP-030")
def test_poc_two_swaps_in_one_batch_are_planned_in_bounded_time(shelf: ExportEnv) -> None:
    """`_ordered` breaks a cycle, then releases the step that was blocked on the cycle's breaker
    again: `ready` keeps a step already emitted. With a second cycle in the batch, `len(emitted)`
    stays below the count and the loop pops the same two steps forever (the list grows without
    bound): the admin's preview never answers and eats the memory of the worker."""
    env = shelf
    export_chapters(env, "Dr Stone", ["1", "2", "3", "4"])
    ids = item_ids_of(env, "Dr Stone")
    name = "Dr Stone/Dr Stone - Ch.{}.cbz".format
    moves = [  # Ch.1 <-> Ch.2 and Ch.3 <-> Ch.4
        ItemMove(ids["Dr Stone - Ch.1.cbz"], name(2)),
        ItemMove(ids["Dr Stone - Ch.2.cbz"], name(1)),
        ItemMove(ids["Dr Stone - Ch.3.cbz"], name(4)),
        ItemMove(ids["Dr Stone - Ch.4.cbz"], name(3)),
    ]
    before = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(10)
    try:
        preview = env.pipeline.rename_preview(env.destination_id, moves=moves)
    except _Hung:
        pytest.fail("the preview of two swaps did not end within 10 s")  # PoC
    except RenameRefusedError:
        return  # refusing a swap is an acceptable answer
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, before)
    assert len(preview.steps) == 6  # each swap: one step through a temporary name, then two
    env.pipeline.apply(preview.plan_id, if_match=preview.base_version, job_id=new_ulid())
    assert record_paths(env) == tree(env) and len(tree(env)) == 4


@pytest.mark.req("EF-EXP-037")
def test_poc_readers_that_appear_after_a_silent_preview_can_be_confirmed(shelf: ExportEnv) -> None:
    """No warning at the preview (nobody read): `announced` is None. A reader appears before the
    application: 409 `confirmation_required`, and the confirmation the admin then sends is refused
    again (`isinstance(announced, dict)` is false): the answer asks for what it can never accept.
    Either the confirmation of the new count goes through, or the answer is `plan.stale`."""
    env = shelf
    export_chapters(env, "Dr Stone", ["1"])
    readers = FakeReaders(count=0)
    env.pipeline.bind_readers(readers)
    moves = [ItemMove(i, f"Dr Stone (2019)/{n}") for n, i in item_ids_of(env, "Dr Stone").items()]
    preview = env.pipeline.rename_preview(env.destination_id, moves=moves)
    assert preview.warnings == ()
    readers.count = 2
    with pytest.raises(RenameConfirmationRequiredError):
        env.pipeline.require_confirmation(preview.plan_id, confirmed=False)
    env.pipeline.require_confirmation(preview.plan_id, confirmed=True)  # PoC: raises again
