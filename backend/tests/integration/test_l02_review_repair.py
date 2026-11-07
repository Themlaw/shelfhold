"""M1b L02 review: the projection repair respects the freeze (EF-INT-026)."""

import pytest

from shelfhold.infrastructure.freeze import FreezeState
from tests.restore_env import restore_env

pytestmark = pytest.mark.req("EF-INT-026")


def test_review_the_projection_repair_writes_nothing_while_the_instance_is_frozen(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from types import SimpleNamespace

    from shelfhold import runtime

    class Repair:
        called = 0

        def __init__(self, *args: object) -> None: ...

        def repair(self) -> None:
            Repair.called += 1

    monkeypatch.setattr(runtime, "MetaProjection", Repair)
    with restore_env() as env:
        me = SimpleNamespace(
            freeze_state=FreezeState(), settings=SimpleNamespace(data_dir=tmp_path)
        )
        with env.engine.begin() as connection:
            me.freeze_state.persist(connection, 4)
        runtime.InstanceRuntime._repair_projections(me, env.engine)  # type: ignore[arg-type]
        assert Repair.called == 0  # frozen: /data is not written
