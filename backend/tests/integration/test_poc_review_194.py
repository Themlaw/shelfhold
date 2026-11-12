"""Adversarial review of PR #194 (M3 L25): proofs of concept, red on the PR head."""

import pytest
import sqlalchemy as sa

from shelfhold.export.application.deletion import DeletionBlockedError
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.library.public import ConfirmationRequiredError, DeletionLevel
from tests.deletion_env import file_hash, make_deletion, make_irreplaceable, spread_unit
from tests.export_env import export_env
from tests.integration.test_deletion_l25 import age_bins, sweeper

LEVEL2 = DeletionLevel.REMOVE_DESTINATIONS
LEVEL3 = DeletionLevel.DELETE_CONTENT


@pytest.mark.req("EF-BIB-031")
def test_poc_sweep_erases_a_unit_that_became_irreplaceable_while_in_the_bin() -> None:
    """EF-BIB-031: retention never deletes an irreplaceable Unit without a typed confirmation.
    Deleted at level 3 while its source still listed it (no word asked), the source then drops
    it: 31 days later the sweep erases the only bytes left, nobody having typed anything."""
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        d.apply(LEVEL3, [unit])  # replaceable now: no typed word needed
        binned = {p: file_hash(p) for p in d.binned()}
        assert binned
        make_irreplaceable(env, unit)  # the source says the chapter is gone
        age_bins(d, 31)
        sweeper(d).sweep()
        assert d.binned() != [], "the sweep erased every copy of an irreplaceable Unit"


@pytest.mark.req("EF-BIB-031")
def test_poc_level_two_takes_the_last_copy_of_an_irreplaceable_unit_without_a_word() -> None:
    """The LAST_COPY guard of level 2 never fires in production: destination rows are always
    `independent = 0` (`record_destination_copy`), so `independent_copies` is the same before and
    after (the PR's own test forces `independent = 1`). An irreplaceable Unit whose /library and
    /store copies are lost keeps one readable file, in the destination: level 2 bins it with no
    confirmation, and the 30-day sweep erases it."""
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        make_irreplaceable(env, unit)
        for root in (env.lib.library, env.lib.store):  # the library disk and /store were lost
            for p in list(root.rglob("*.cbz")):
                if "Kavita" not in p.parts and ".shelfhold" not in p.parts:
                    p.unlink()
        with env.engine.begin() as connection:
            connection.execute(sa.text("DELETE FROM replica WHERE location IN ('library','store')"))
        [last] = list((env.lib.manga / "Kavita").rglob("*.cbz"))
        assert last.stat().st_size > 0  # the only bytes there are
        plan = d.service.preview(LEVEL2, [unit])
        assert plan.units[0].irreplaceable
        with pytest.raises((DeletionBlockedError, ConfirmationRequiredError)):
            d.apply(LEVEL2, [unit], plan=plan)


@pytest.mark.req("EF-BIB-021")
def test_poc_level_two_is_undone_by_the_next_export() -> None:
    """Probe: after "take out of the destinations", does the next export run put it back?"""
    with export_env() as env:
        d = make_deletion(env)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        d.apply(LEVEL2, [unit])
        assert list((env.lib.manga / "Kavita").rglob("*.cbz")) == []
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert list((env.lib.manga / "Kavita").rglob("*.cbz")) == [], "re-exported"
