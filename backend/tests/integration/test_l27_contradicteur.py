"""M3 L27, contradicteur de la #234 (Opus) : PoC rouges contre `source migrate-ids`.

Chaque test décrit ce que la spec promet (EF-SRC-022, EF-BIB-054 (4), carte L27 : « intouchable :
pierres tombales ») et échoue tant que le code de production ne le tient pas. Références
synthétiques seulement (`source-a`, `plugin-a`)."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import acquisition
from shelfhold.library.public import SeriesBindingRef, TombstoneService
from shelfhold.ops.source_migration import SourceMigrationOps
from tests.integration.test_l27_source_aliases import (
    GONE,
    PLUGIN,
    SOURCE,
    alias,
    erase,
    ids,
    iid,
    item_ref,
    live,
    make_ops,
    plan,
)
from tests.l50_env import Seeded, seeded

pytestmark = pytest.mark.req("EF-SRC-022", "EF-BIB-054")


def gate(env: Seeded, remote_id: str) -> bool:
    """What every door asks before following, discovering or acquiring a series
    (`follow.py`, `discovery.py`, `acquire.py`, `tree_target.py`): `check(SeriesBindingRef)` on a
    freshly replayed service."""
    service: TombstoneService = SourceMigrationOps(env.inst.comp)._service()
    return service.check(SeriesBindingRef(PLUGIN, SOURCE, remote_id), root_id=None).blocked


# -- A (bloquant) : après la migration, la forme nouvelle d'une série effacée passe les portes ----


@pytest.mark.req("EF-SRC-022", "EF-BIB-054")
@pytest.mark.parametrize("by", ["plugin", "table"])
def test_poc_erased_series_comes_back_under_its_new_id_after_the_migration(
    by: str, monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """La source change de schéma : la série effacée `gone-1` s'appelle désormais `gone-new`, et
    la source ne parle plus que le nouveau schéma. La migration bloque bien la ligne de `gone-1`
    (sa propre forme est tombée), mais ne laisse rien qui rattache `gone-new` à la pierre tombale :
    aucun alias écrit pour un bloqué, aucune porte qui consulte l'alias inverse. La série
    effacée se suit de nouveau sous son nouvel identifiant (EF-BIB-054 (4) : « l'alias inverse
    (forme ancienne ↔ forme nouvelle) est passé au test d'appartenance »)."""
    with seeded(settings, tmp_path / "inst") as env:
        erased = live(env, GONE)
        erase(env, monkeypatch, settings, erased)
        clean = live(env, "old-1")
        if by == "plugin":
            ops = make_ops(env, {GONE: "gone-new", "old-1": "new-1"})
        else:
            alias(env, GONE, "gone-new")
            alias(env, "old-1", "new-1")
            ops = make_ops(env)
        assert gate(env, GONE)  # before: the erased series is held under its own id
        report = plan(ops)
        assert iid(env, clean) in ids(report, "migrate")
        plan(ops, report["fingerprint"])
        assert item_ref(env, clean) == "new-1"
        # after: the source only speaks the new scheme; the erased series must stay erased
        assert gate(env, "gone-new"), "the erased series is followable again under its new id"


# -- B (majeur) : une chaîne qui passe par un alias d'un autre schéma n'est pas suivie ----------


@pytest.mark.req("EF-SRC-022", "EF-BIB-054")
def test_poc_alias_of_another_scheme_leading_to_an_erased_series_is_not_screened(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """`hit-x` → `mid-x` (schéma visé), puis `mid-x` → `gone-1` (un autre schéma) : `mid-x` est
    une autre forme de la série effacée. Le plan suit la chaîne du seul schéma visé, et la
    famille ne prend que l'inverse : la suite avant d'un autre schéma n'est jamais criblée.
    La ligne migre sur `mid-x`, et `mid-x` passe toutes les portes."""
    with seeded(settings, tmp_path / "inst") as env:
        erased = live(env, GONE)
        erase(env, monkeypatch, settings, erased)
        hit = live(env, "hit-x", chapters=("1",))
        alias(env, "hit-x", "mid-x")
        with env.inst.engine.begin() as connection:
            connection.execute(
                sa.insert(acquisition.source_alias).values(
                    plugin_id=PLUGIN, old_source_key=SOURCE, old_remote_id="mid-x",
                    new_source_key=SOURCE, new_remote_id=GONE, id_scheme="legacy",
                )
            )  # fmt: skip
        report = plan(make_ops(env))
        assert iid(env, hit) in ids(report, "blocked"), "a form of the erased series migrates"
        assert iid(env, hit) not in ids(report, "migrate")


# -- C (mineur) : des graphies que le repli laisse passer ---------------------------------------


@pytest.mark.req("EF-SRC-022", "EF-BIB-054")
@pytest.mark.parametrize(
    "spelling",
    [
        "/gone-1",  # a leading separator (a relative URL re-typed)
        "gone­-1",  # a soft hyphen: invisible, kept by NFKC
        "gone​-1",  # a zero-width space: invisible, kept by NFKC
        "gone-1?",  # an empty query
        "gone-1#",  # an empty fragment
    ],
)
def test_poc_spellings_of_an_erased_reference_that_the_fold_lets_through(
    spelling: str, monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """`scopes.fold_ref` promet « dans le doute, on bloque » et range `GONE-1`, `gone-1/`,
    `gone%2D1` avec `gone-1` ; ces graphies-ci, aussi proches, passent."""
    with seeded(settings, tmp_path / "inst") as env:
        erased = live(env, GONE)
        erase(env, monkeypatch, settings, erased)
        victim = live(env, "old-5")
        report = plan(make_ops(env, {"old-5": spelling}))
        assert iid(env, victim) in ids(report, "blocked"), spelling
