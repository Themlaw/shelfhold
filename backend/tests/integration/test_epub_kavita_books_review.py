"""M2 L09, review of #140: each attack becomes a regression test that fails if it succeeds.

A first EPUB lands in Kavita as an "unknown" series until its age is pushed: an account restricted
below its age that includes the unknowns would see it. A series Kavita split in two must be locked
on both halves. An age that reads below the wanted one is a lasting alert. The lock is read again
right before each write. Synthetic Kavita, no network.
"""

from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.domain.planner import AGE_WAIT
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.ops.status import collect
from tests.epub_export_env import (
    EVERYONE_10,
    TEEN,
    UNKNOWN,
    add_book,
    books_library,
    export_round,
    kavita_scans,
    open_gate,
    visible_to_restricted,
    written,
)
from tests.export_env import ExportEnv, Series, export_env
from tests.fakes.kavita.server import member
from tests.family_env import family_env, rate
from tests.kavita_export_env import LIBRARY_ID, Wired, wire

pytestmark = pytest.mark.req("EF-EXP-040", "EF-EXP-033")


def _seed(env: ExportEnv, n: int = 2, age: int = 10) -> Series:
    books_library(env)
    book = env.new_series("Le Livre")
    for i in range(1, n + 1):
        add_book(env, book, str(i))
    rate(env, book, age)
    return book


def _kid(w: Wired, *, include_unknowns: bool, age_rating: int = TEEN) -> None:
    w.fake.users.append(
        member(
            "kid", libraries=(LIBRARY_ID,), age_rating=age_rating,
            include_unknowns=include_unknowns,
        )
    )  # fmt: skip


def _blocked(env: ExportEnv) -> list[str]:
    return sorted(b.code for b in env.pipeline.preview(env.destination_id).blocked)


def _states(env: ExportEnv) -> list[str]:
    with env.engine.connect() as connection:
        return sorted(i.state for i in collect(connection))


def test_first_epub_withheld_from_restricted_account_that_includes_unknowns() -> None:
    """A1/A5: a child restricted to 13+ who includes the unknowns would read a 16+ first EPUB
    while Kavita still has it "unknown": it is not written, and the alert names the setting."""
    with export_env() as env:  # an adult shelf the child also reaches
        w = wire(env)
        _seed(env, n=2, age=16)
        _kid(w, include_unknowns=True)
        assert visible_to_restricted(UNKNOWN, ceiling=TEEN, include_unknowns=True)
        assert export_round(w) == [] and env.files() == []
        (alert,) = w.events.details("target.child_exposure")
        assert alert["setting"] == "include_unknowns" and alert["accounts"] == ["kid"]
        assert _blocked(env) == [AGE_WAIT, AGE_WAIT]

        w.fake.users[-1]["ageRestriction"]["includeUnknowns"] = False  # the admin fixed it
        assert len(export_round(w)) == 1


def test_first_epub_at_or_below_the_unknowns_tier_still_goes() -> None:
    with family_env(13) as env:
        w = wire(env)
        _seed(env, n=1, age=13)
        _kid(w, include_unknowns=True)  # restricted to 13+: a 13+ book is its own anyway
        assert len(export_round(w)) == 1


def test_series_split_in_kavita_holds_the_series() -> None:
    """A7: Kavita reads two series in the folder; the second is unknown and unlocked. Nothing
    more of the series goes, and the split is listed by `status`."""
    with family_env(13) as env:
        w = wire(env)
        book = _seed(env, n=2)
        export_round(w)
        sid = kavita_scans(w, "Le Livre")
        open_gate(w)
        (second,) = export_round(w)
        folder = written(env)[0].rsplit("/", 1)[0]
        other = w.fake.add_series("Le Livre bis", f"/manga/Kavita/{folder}", [w.remote(second)])
        w.fake.files[sid] = [f for f in w.fake.files[sid] if not f.endswith(second)]
        record = export.export_record
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(record).where(record.c.rel_path == second)
                .values(remote_ref=str(other), state="resolved")
            )  # fmt: skip
        add_book(env, book, "3")
        facts = open_gate(w)
        assert facts.verified == frozenset()
        assert export_round(w) == [] and len(written(env)) == 2
        assert "series_split" in _states(env)
        assert [d["cause"] for d in w.events.details("reconcile.drift")] == ["series_split"]
        assert w.fake.ages[other]["ageRatingLocked"] is False


def test_lock_lost_between_read_back_and_write_refuses_the_write() -> None:
    """A1: the lock is read again under the locks, right before the write."""
    with family_env(13) as env:
        w = wire(env)
        _seed(env, n=2)
        export_round(w)
        sid = kavita_scans(w, "Le Livre")
        assert open_gate(w).verified
        w.fake.ages[sid].update(ageRating=UNKNOWN, ageRatingLocked=False)  # a scan reset it
        report = env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert report.ops == () or len(written(env)) == 1
        assert len(written(env)) == 1
        assert len(export_round(w)) == 1  # pushed again and read back: it goes
        assert w.fake.ages[sid]["ageRatingLocked"] and w.fake.ages[sid]["ageRating"] == EVERYONE_10


def test_upward_reclassification_whose_push_fails_is_a_lasting_alert() -> None:
    """Minor 3: the age reads below the wanted one: `series_age_stale` in `status` until fixed."""
    with family_env(13) as env:
        w = wire(env)
        book = _seed(env, n=1)
        export_round(w)
        sid = kavita_scans(w, "Le Livre")
        open_gate(w)
        rate(env, book, 13)
        w.fake.remove_route("POST", "/api/Series/metadata")
        assert not open_gate(w).open
        assert "series_age_stale" in _states(env)
        assert "series_age_stale" in _states(env)  # still there at the next look
        w.fake.missing.clear()
        w.rebuild_client()
        assert open_gate(w).verified and w.fake.ages[sid]["ageRating"] == TEEN
        assert "series_age_stale" not in _states(env)


def test_two_jobs_holding_the_same_open_gate_send_one_first_epub() -> None:
    """A2: a second run with the facts of the first adds no second first EPUB."""
    with family_env(13) as env:
        w = wire(env)
        _seed(env, n=3)
        facts = open_gate(w)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        env.pipeline.bind_age_gate(facts)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert len(written(env)) == 1


def test_kavita_below_the_floor_closes_the_gate_on_an_adult_shelf() -> None:
    """A6: 0.9.0.3 is below the 0.9.1 floor of ENF-COMP-001."""
    with export_env() as env:
        w = wire(env, version="0.9.0.3")
        _seed(env, n=1)
        assert not open_gate(w).open
        assert export_round(w) == [] and env.files() == []


def test_crash_between_registry_and_push_heals_at_the_next_round() -> None:
    """A8: the registry ahead of Kavita is drift; the next round pushes and reads back."""
    with family_env(13) as env:
        w = wire(env)
        _seed(env, n=2)
        export_round(w)
        sid = kavita_scans(w, "Le Livre")
        real = w.client.set_series_age

        async def killed(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("process killed")

        w.client.set_series_age = killed  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            open_gate(w)
        w.client.set_series_age = real  # type: ignore[method-assign]
        assert len(export_round(w)) == 1
        assert w.fake.ages[sid]["ageRatingLocked"] and w.fake.ages[sid]["ageRating"] == EVERYONE_10


# -- counter-review of #140: the bypasses of the first fixes -------------------------------------


def test_account_added_after_the_gate_opened_is_read_again_before_the_first_epub() -> None:
    """B1: the accounts are read again under the locks: a child added since the gate opened
    withholds the first EPUB, and the report does not list the write it refused."""
    with export_env() as env:
        w = wire(env)
        _seed(env, n=2, age=16)
        assert open_gate(w).unknowns_tier is None
        _kid(w, include_unknowns=True)
        report = env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert written(env) == [] and env.files() == []
        assert report.ops == ()


def test_include_unknowns_switched_on_after_the_gate_opened_withholds_the_first_epub() -> None:
    """B1b"""
    with export_env() as env:
        w = wire(env)
        _seed(env, n=2, age=16)
        _kid(w, include_unknowns=False)
        open_gate(w)
        w.fake.users[-1]["ageRestriction"]["includeUnknowns"] = True
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert written(env) == []


def test_account_reaching_the_files_by_another_library_withholds_the_first_epub() -> None:
    """B2: another Kavita library over the same folder reaches the files just as well."""
    with export_env() as env:
        w = wire(env)
        _seed(env, n=2, age=16)
        wider = {**w.fake.libraries[0], "id": 9, "name": "Tout", "folders": ["/manga"]}
        w.fake.libraries.append(wider)
        w.fake.users.append(member("kid", libraries=(9,), age_rating=TEEN, include_unknowns=True))
        assert export_round(w) == [] and written(env) == []
        assert w.events.details("target.child_exposure")[0]["setting"] == "include_unknowns"


def test_split_holds_when_the_other_half_leaves_resolved() -> None:
    """B3: a rescan or a rewrite takes the other half out of `resolved`, not out of Kavita: the
    series stays split while Kavita shows two series for it."""
    with family_env(13) as env:
        w = wire(env)
        book = _seed(env, n=2)
        export_round(w)
        sid = kavita_scans(w, "Le Livre")
        open_gate(w)
        (second,) = export_round(w)
        folder = written(env)[0].rsplit("/", 1)[0]
        other = w.fake.add_series("Le Livre bis", f"/manga/Kavita/{folder}", [w.remote(second)])
        w.fake.files[sid] = [f for f in w.fake.files[sid] if not f.endswith(second)]
        record = export.export_record
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(record).where(record.c.rel_path == second)
                .values(remote_ref=str(other), state="resolved")
            )  # fmt: skip
        add_book(env, book, "3")
        assert open_gate(w).verified == frozenset() and "series_split" in _states(env)
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(record).where(record.c.rel_path == second).values(state="scanned")
            )
        assert open_gate(w).verified == frozenset()
        assert export_round(w) == [] and len(written(env)) == 2
        assert "series_split" in _states(env)

        # Kavita merges the halves again: only one series shows, the split lifts itself
        w.fake.series = [s for s in w.fake.series if s["id"] != other]
        del w.fake.ages[other], w.fake.files[other]
        w.fake.files[sid].append(w.remote(second))
        assert open_gate(w).verified
        assert "series_split" not in _states(env)
        assert len(export_round(w)) == 1
