"""L12, first commit (EF-BIB-055, EF-BIB-037): the gate of the new Edition, before any attach.

An Edition that arrives (ingest of a source, an attach by external id, its undo, a relink) gets a
shelf through one door, `file_new_edition`. A shelf below 18+ never takes an Edition nobody rated,
nor one a suggested age exceeds: it stays unfiled and waits in "À classer". It enters only after an
admin rates it (`rate_edition`) or rates the Work that holds it, and is then filed explicitly. The
production pipeline is the proof: nothing reaches the library the children read.
"""

import ast
import re
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import Filing, file_new_edition
from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.application.to_rate import to_rate
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog
from tests.export_env import ExportEnv
from tests.family_env import (
    add_adult_shelf,
    add_edition_to,
    family_env,
    rate,
    rate_edition,
    suggest,
)

pytestmark = pytest.mark.req("EF-BIB-055", "EF-BIB-037")

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"


def shelf_of(env: ExportEnv, edition_id: str) -> str | None:
    with env.engine.connect() as connection:
        value = connection.execute(
            sa.select(catalog.edition.c.shelf_id).where(catalog.edition.c.id == edition_id)
        ).scalar_one()
    return None if value is None else str(value)


def gate(env: ExportEnv, edition_id: str, shelf_id: str | None, *, hold: bool = False) -> Filing:
    with env.engine.begin() as connection:
        return file_new_edition(connection, edition_id, shelf_id, hold_unrated_work=hold)


def run(env: ExportEnv) -> int:
    return len(env.pipeline.run(env.destination_id, job_id=new_ulid()).ops)


def test_unrated_new_edition_never_enters_family_shelf() -> None:
    """Cas de l'arrivée (ingest): a new Edition of a Work rated for the family shelf is not filed
    on it, waits in "À classer", and enters only once rated and filed."""
    with family_env(13) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        later = add_edition_to(env, series)  # arrives after the rating, filed nowhere
        filing = gate(env, later, env.shelf_id)
        assert filing == Filing(None, waiting=True, reason="unrated_edition")
        assert shelf_of(env, later) is None
        with env.engine.connect() as connection:
            assert series.work_id in [r.work_id for r in to_rate(connection)]
        assert run(env) == 1  # only the Edition rated with the Work
        rate_edition(env, later, 10)
        assert gate(env, later, env.shelf_id) == Filing(env.shelf_id, waiting=False, reason=None)


def test_gate_files_on_an_adult_shelf_at_once() -> None:
    with family_env(13) as env:
        adult_shelf, _ = add_adult_shelf(env)
        series = env.new_series(shelf_id=adult_shelf)
        later = add_edition_to(env, series)
        assert gate(env, later, adult_shelf) == Filing(adult_shelf, waiting=False, reason=None)
        assert shelf_of(env, later) == adult_shelf


def test_gate_refuses_a_suggested_age_above_the_rating() -> None:
    with family_env(13) as env:
        series = env.new_series()
        rate(env, series, 10)
        later = add_edition_to(env, series)
        rate_edition(env, later, 10)
        suggest(env, later, 18, x=True)  # a sub-source says nsfw
        filing = gate(env, later, env.shelf_id)
        assert filing == Filing(None, waiting=True, reason="suggestion_above")
        assert shelf_of(env, later) is None


def test_gate_never_holds_an_unrated_edition_of_a_rated_work() -> None:
    """Holding an Edition of a Work nobody rated (EF-EXP-050) is for a Work that waits as a
    whole; an Edition new to a rated Work is never held on the family shelf, even when asked."""
    with family_env(13) as env:
        series = env.new_series()
        rate(env, series, 10)
        later = add_edition_to(env, series)
        assert gate(env, later, env.shelf_id, hold=True).shelf_id is None


def test_gate_holds_only_when_asked_for_a_work_nobody_rated() -> None:
    with family_env(13) as env:
        fresh = env.new_series(shelf_id="")  # a new Work, nobody rated it
        assert gate(env, fresh.edition_id, env.shelf_id).shelf_id is None
        held = gate(env, fresh.edition_id, env.shelf_id, hold=True)
        assert held == Filing(env.shelf_id, waiting=True, reason="unrated_edition")
        env.add_chapter(fresh, "1")
        assert run(env) == 0  # held: the barrier of the planner refuses it all the same


def test_explicit_filing_uses_the_same_admission() -> None:
    with family_env(13) as env:
        series = env.new_series()
        rate(env, series, 10)
        later = add_edition_to(env, series)
        shelves = ShelfService(env.engine, clock=env.clock)
        assert shelves.file_edition(later, env.shelf_id).reason == "unrated_edition"
        rate_edition(env, later, 10)
        assert shelves.file_edition(later, env.shelf_id).filed


# Every writer of `edition.shelf_id` goes through the gate, or says here why it need not.
_OTHER_WRITERS = {
    # the gate itself
    "catalog/application/new_edition_gate.py",
    # an explicit filing by an admin: the same admission, never a hold (EF-BIB-055)
    "export/application/shelves.py",
    # unshelving: a withdrawal only ever takes an Edition out
    "export/application/withdrawal.py",
    # a new Work, nobody rated it, held until rated on the shelf of the follow (EF-EXP-050)
    "acquisition/application/discovery.py",
    # a new Work whose files already are in that library: held, follow suspended (EF-EXP-048)
    "export/targets/kavita/adopt.py",
}
_EDITION_TABLE = re.compile(r"\b(?:catalog\.)?edition\)")


def _writers() -> set[str]:
    found = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "values" or not any(k.arg == "shelf_id" for k in node.keywords):
                continue
            if _EDITION_TABLE.search(ast.unparse(node.func.value)):
                found.add(path.relative_to(SRC).as_posix())
    return found


def test_every_writer_of_the_shelf_of_an_edition_goes_through_the_gate() -> None:
    assert _writers() <= _OTHER_WRITERS
    assert "catalog/application/relink.py" not in _writers()  # relink files through the gate
