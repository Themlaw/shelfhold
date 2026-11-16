"""EF-EXP-047, EF-EXP-065, EF-EXP-002 (M4 L09): a shelf below 18+ is never linked to a CWA drop
box, which keeps no account apart: refused **when it is bound** and again **when it is exported**.

The admin's attestation counts for nothing here (the drop box is first-party and says `none`);
the ceiling lowered after the binding makes the pass refuse, and nothing is dropped.
"""

import pytest

from shelfhold.export.application.fan_out import EpubFile
from shelfhold.export.domain.bindings import (
    BindingError,
    TargetKindTransportError,
    UnpartitionedDestinationError,
)
from tests.cwa_env import (
    INGEST,
    add_book,
    bind_cwa,
    cwa_target,
    lower_ceiling,
    names,
    records,
    rig,
)
from tests.export_env import export_env
from tests.fan_out_env import service


@pytest.mark.req("EF-EXP-047")
@pytest.mark.parametrize("ceiling", [13, 10, 6])
def test_cwa_destination_refused_for_family_shelf(ceiling: int) -> None:
    with export_env(max_age_rating=ceiling) as env:
        for partitioning in ("none", "library", "user"):  # whatever the target claims
            target = cwa_target(env, partitioning=partitioning)
            (env.lib.manga / INGEST).mkdir(exist_ok=True)
            with pytest.raises(UnpartitionedDestinationError) as refused:
                service(env).bind_destination(
                    shelf_id=env.shelf_id, target_id=target, transport="dropbox",
                    plugin_egress="foyer", root_subpath=INGEST,
                )  # fmt: skip
            assert refused.value.code == "shelf.destination_not_partitioned"
        with env.engine.connect() as connection:
            bound = connection.exec_driver_sql(
                "SELECT count(*) FROM shelf_destination d JOIN export_target t "
                "ON t.id = d.target_id WHERE t.kind = 'cwa'"
            ).scalar_one()
        assert bound == 0


@pytest.mark.req("EF-EXP-047")
@pytest.mark.parametrize("ceiling", [18, 99, None])
def test_an_adult_shelf_may_be_bound_to_a_cwa_drop_box(ceiling: int | None) -> None:
    with export_env(max_age_rating=ceiling) as env:
        assert bind_cwa(env)


@pytest.mark.req("EF-EXP-047", "EF-EXP-065")
def test_the_ceiling_lowered_after_the_binding_stops_the_export() -> None:
    with export_env(max_age_rating=18) as env:
        store: dict[str, EpubFile] = {}
        add_book(env, env.new_series("Synthetic Book"), "1", store)
        destination = bind_cwa(env)
        fan, _source = rig(env, store)
        lower_ceiling(env, 13)  # the shelf became a family shelf
        outcome = next(
            o for o in fan.run(env.shelf_id, "c1").outcomes if o.destination_id == destination
        )
        assert outcome.status == "refused" and outcome.code == "shelf.destination_not_partitioned"
        assert names(env) == [] and records(env, destination) == []
        lower_ceiling(env, 18)  # and back: the same binding exports again, nothing was lost
        again = next(
            o for o in fan.run(env.shelf_id, "c2").outcomes if o.destination_id == destination
        )
        assert (again.status, again.executed) == ("done", 1)


@pytest.mark.req("EF-EXP-047", "EF-EXP-065")
def test_a_family_shelf_is_refused_even_if_the_admin_attested_the_drop_box() -> None:
    with export_env(max_age_rating=18) as env:
        store: dict[str, EpubFile] = {}
        add_book(env, env.new_series("Synthetic Book"), "1", store)
        target = cwa_target(env, partitioning="library")  # attested by `add_target`
        destination = bind_cwa(env, target_id=target)
        fan, _source = rig(env, store)
        lower_ceiling(env, 10)
        outcome = next(
            o for o in fan.run(env.shelf_id, "c1").outcomes if o.destination_id == destination
        )
        assert outcome.status == "refused" and names(env) == []


@pytest.mark.req("EF-EXP-022", "EF-EXP-058")
@pytest.mark.parametrize("transport", ["tree", "pull", "push", "carrier"])
def test_a_cwa_target_is_bound_only_as_a_drop_box(transport: str) -> None:
    with export_env() as env:
        (env.lib.manga / INGEST).mkdir(exist_ok=True)
        with pytest.raises(BindingError) as refused:
            service(env).bind_destination(
                shelf_id=env.shelf_id, target_id=cwa_target(env), transport=transport,
                plugin_egress="foyer", root_subpath=INGEST,
            )  # fmt: skip
        assert isinstance(refused.value, TargetKindTransportError)
        assert refused.value.code == "target.transport_not_supported"
