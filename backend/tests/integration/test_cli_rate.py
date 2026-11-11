"""L11 (EF-INT-014, EF-BIB-036, EF-INT-023, EF-EXP-048): `shelfhold rate`, the real command over a
real instance, and the two states it leaves.

`rate` is a reserved action (fresh session or the host's console, never a key) that goes through
`RatingService`: the audit entry is written in the transaction of the rows. A raised age that no
longer fits a shelf below 18+ prints the plan of the files to withdraw and withdraws none. Rating
all the Works that wait asks for the confirmation to be typed and audits each Work.
"""

import re
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.follow_suspended import suspend
from shelfhold.infrastructure.schema import export
from tests.integration.test_family_shelves_real import dedicated_adult_library, family, link
from tests.l54_env import SERIES, Bench54, bench54


def held_work(b: Bench54, title: str) -> str:
    """A Work nobody rated, waiting out of every shelf (held until rated): its id."""
    series = b.env.new_series(title)
    with b.engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE edition SET shelf_id = NULL WHERE id = :e"), {"e": series.edition_id}
        )
    return series.work_id


def with_family_shelf(b: Bench54) -> str:
    dedicated_adult_library(b.env)
    destination = family(b.env)
    link(b.env, destination)
    return str(
        b.rows("SELECT shelf_id FROM shelf_destination WHERE id = :d", d=destination)[0]["shelf_id"]
    )


def _audits(b: Bench54, action: str = "rate") -> list[str]:
    return [r["target"] for r in b.rows("SELECT target FROM audit WHERE action = :a", a=action)]


@pytest.mark.req("EF-INT-014", "EF-BIB-036")
def test_cli_rate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        work = b.env.new_series("Dr Stone").work_id
        capsys.readouterr()
        assert b.cli("rate", "13+", "--work", work) == 0
        assert f"work:{work}" in capsys.readouterr().out
        (row,) = b.rows("SELECT age_rating, age_x, rated_at FROM work WHERE id = :w", w=work)
        assert (row["age_rating"], row["age_x"]) == (13, 0) and row["rated_at"] is not None
        assert _audits(b) == [f"work:{work}"]  # written with the rows
        assert b.cli("rate", "X", "--work", work) == 0  # X is its own flag on the 18+ tier
        (row,) = b.rows("SELECT age_rating, age_x FROM work WHERE id = :w", w=work)
        assert (row["age_rating"], row["age_x"]) == (18, 1)
        edition = b.env.new_series("Monster").edition_id
        assert b.cli("rate", "10+", "--edition", edition) == 0
        (ed,) = b.rows("SELECT age_rating, rated_at FROM edition WHERE id = :e", e=edition)
        assert ed["age_rating"] == 10 and ed["rated_at"] is not None
        capsys.readouterr()
        before = len(_audits(b))
        for bad in (("7+", "--work", work), ("13+", "--work", "01NOSUCHWORK00000000000000")):
            assert b.cli("rate", *bad) != 0  # not on the scale of tiers; no such Work
            assert "Refusé" in capsys.readouterr().err
        assert b.cli("rate", "13+") != 0  # nothing named
        assert len(_audits(b)) == before  # a refusal leaves no trace


@pytest.mark.req("EF-INT-014", "EF-BIB-037")
def test_cli_rate_raising_the_age_prints_the_withdrawal_plan_and_applies_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        shelf = with_family_shelf(b)
        series = b.env.new_series("Lupin")
        with b.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE edition SET shelf_id = :s WHERE id = :e"),
                {"s": shelf, "e": series.edition_id},
            )
        assert b.cli("rate", "10+", "--work", series.work_id) == 0  # fits the family shelf
        capsys.readouterr()
        assert b.cli("rate", "16+", "--work", series.work_id) == 0
        err = capsys.readouterr().err
        assert series.edition_id in err and "above_ceiling" in err
        assert "shelfhold shelf withdraw" in err  # the way to apply it, with its own confirmation
        (edition,) = b.rows("SELECT shelf_id FROM edition WHERE id = :e", e=series.edition_id)
        assert edition["shelf_id"] == shelf  # nothing was withdrawn by itself


@pytest.mark.req("EF-INT-014", "EF-EXP-048")
def test_rate_batch_requires_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        with_family_shelf(b)
        works = [held_work(b, title) for title in ("Akira", "Lupin", "Monster")]
        capsys.readouterr()
        assert b.cli("rate", "13+", "--pending") == 2  # the list and what to type
        shown = capsys.readouterr().out
        assert all(w in shown for w in works)
        token = re.search(r"--confirm (\S+)", shown)
        assert token and token.group(1).startswith("3-")  # the count is part of what is typed
        assert b.rows("SELECT count(*) AS n FROM work WHERE rated_at IS NOT NULL")[0]["n"] == 0
        assert b.cli("rate", "13+", "--pending", "--confirm", "3-00000000") == 2  # not that one
        assert b.rows("SELECT count(*) AS n FROM work WHERE rated_at IS NOT NULL")[0]["n"] == 0
        assert b.cli("rate", "16+", "--pending", "--confirm", token.group(1)) == 2  # other age
        held_work(b, "Naruto")  # the list moved since it was read
        assert b.cli("rate", "13+", "--pending", "--confirm", token.group(1)) == 2
        assert _audits(b) == []
        capsys.readouterr()
        assert b.cli("rate", "13+", "--pending") == 2
        fresh = re.search(r"--confirm (\S+)", capsys.readouterr().out)
        assert fresh and fresh.group(1).startswith("4-")
        assert b.cli("rate", "13+", "--pending", "--confirm", fresh.group(1)) == 0
        assert len(_audits(b)) == 4 and len(set(_audits(b))) == 4  # one entry per Work
        assert b.rows("SELECT count(*) AS n FROM work WHERE age_rating = 13")[0]["n"] == 4
        assert b.cli("rate", "13+", "--pending") == 0  # nothing waits any more


@pytest.mark.req("EF-INT-014", "EF-INT-023")
def test_cli_rate_exits_rating_pending_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        with_family_shelf(b)
        work = held_work(b, "Lupin")
        shown = b.status(capsys)
        assert "rating_pending" in shown and f"shelfhold rate <age> --work {work}" in shown
        assert b.cli("rate", "10+", "--work", work) == 0
        assert "rating_pending" not in b.status(capsys)


@pytest.mark.req("EF-EXP-048", "EF-INT-023")
def test_follow_skips_a_suspended_work_until_it_is_rated(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """The suspension is per Work: the follow cycle asks for nothing of it, then goes on once an
    admin rated it, through the same door as for every other state."""
    with bench54(monkeypatch, settings, tmp_path) as b:
        b.upstream({1: "One", 2: "Two"})
        b.follow()
        (found,) = b.rows("SELECT id FROM work WHERE title = :t", t=SERIES)
        work = found["id"]
        asked = len(b.downloaded())
        with b.engine.begin() as connection:
            shelf = connection.execute(sa.select(export.shelf.c.id)).scalar_one()
            suspend(connection, shelf, [work])
        b.upstream({1: "One", 2: "Two", 3: "Three", 4: "Four"})
        b.follow()
        assert len(b.downloaded()) == asked  # chapters 3 and 4 are not asked for
        assert "follow_suspended" in b.status(capsys)
        assert b.cli("rate", "18+", "--work", work) == 0
        b.follow()
        assert len(b.downloaded()) == asked + 2  # now they are
        assert "follow_suspended" not in b.status(capsys)


@pytest.mark.req("EF-INT-014", "EF-BIB-055")
def test_cli_rate_work_lists_the_higher_edition_suggestions_it_leaves_open(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    from shelfhold.infrastructure.ids import new_ulid

    with bench54(monkeypatch, settings, tmp_path) as b:
        shelf = with_family_shelf(b)
        series = b.env.new_series("Kids title")
        with b.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE edition SET shelf_id = :s WHERE id = :e"),
                {"s": shelf, "e": series.edition_id},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO age_suggestion (id, entity_type, entity_id, value, x, provenance,"
                    " seen_at) VALUES (:i, 'edition', :e, 18, 1, 'sub_source', CURRENT_TIMESTAMP)"
                ),
                {"i": new_ulid(), "e": series.edition_id},
            )
        capsys.readouterr()
        assert b.cli("rate", "10+", "--work", series.work_id) == 0
        err = capsys.readouterr().err
        assert series.edition_id in err and "sub_source" in err and "X" in err
        assert b.rows("SELECT state FROM age_suggestion")[0]["state"] == "open"
        # the Work is rated, but the Edition still waits: the list shows it, the batch skips it
        assert b.cli("rate", "13+", "--pending") == 0
        captured = capsys.readouterr()
        assert "Aucune" in captured.out and series.edition_id in captured.err
        assert b.cli("rate", "10+", "--edition", series.edition_id) == 0
        assert b.rows("SELECT state FROM age_suggestion")[0]["state"] == "rejected"
