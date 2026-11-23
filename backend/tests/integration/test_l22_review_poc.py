"""Relecture adverse de L22 : le contrôle de disposition au rattachement d'un Komga.

`bind_destination` lit les fichiers de l'arbre par `tree_place(...)[0]`, qui est la racine
physique de l'arbre et non la liaison primaire : la requête sur `export_record` ne trouve jamais
rien et le contrôle ADR-0052 ne voit aucun fichier.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
import sqlalchemy as sa

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.export.application.shelves import ShelfService
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.export_env import export_env
from tests.fakes.komga import BASE_URL as KOMGA_URL
from tests.fan_out_env import add_target, service
from tests.integration.test_l22_komga_pass_and_audit import add_komga_target, komga_instance

JOB = "01J00000000000000000000000"


@pytest.mark.req("EF-EXP-020")
def test_komga_layout_check_sees_the_files_of_the_tree_it_indexes() -> None:
    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        env.pipeline.run(env.destination_id, job_id=JOB)
        with env.engine.connect() as connection:
            written = sorted(
                connection.execute(
                    sa.select(export.export_record.c.rel_path).where(
                        export.export_record.c.shelf_destination_id == env.destination_id,
                        export.export_record.c.rel_path.is_not(None),
                    )
                ).scalars()
            )
        assert written  # the primary wrote a file in its tree
        tree_id = {b.destination_id: b.tree_id for b in service(env).list_bindings(env.shelf_id)}[
            env.destination_id
        ]
        seen: list[list[str]] = []

        def spy(rel_paths: Sequence[str]) -> list[object]:
            seen.append(sorted(rel_paths))
            return []

        komga = add_target(env, kind="komga", partitioning="none", name="Komga")
        ShelfService(env.engine, clock=env.clock, layout_check=spy).bind_destination(  # type: ignore[arg-type]
            shelf_id=env.shelf_id, target_id=komga, transport="tree", plugin_egress="foyer",
            root_subpath="Kavita", attach_to_tree=tree_id,
        )  # fmt: skip
        assert seen == [written]  # the rule is asked about the files the tree holds


# -- the daily audit stops at the first Komga whose key cannot be opened ------------------------


@pytest.mark.req("EF-EXP-047", "ENF-SEC-016")
def test_a_komga_without_sealed_key_never_stops_the_audit_of_the_others(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`connected_komga_clients` says a target without a sealed key is skipped; the vault raises
    `LookupError` (not `KeyError`) for a row that holds no secret, and the generator dies: the
    audit of every later Komga is not run, its proof goes stale without an alert."""
    with komga_instance(settings, tmp_path, monkeypatch) as inst:
        keyless = new_ulid()
        with write_transaction(inst.engine) as connection:  # type: ignore[arg-type]
            connection.execute(
                sa.insert(export.export_target).values(
                    id="0" + keyless[1:], kind="komga", name="Sans clé", config={"url": KOMGA_URL},
                    path_map=[{"local": str(inst.library), "remote": "/manga", "mode": "rw"}],
                )
            )  # fmt: skip
        sealed = add_komga_target(inst)
        clients = list(rj.connected_komga_clients(inst.comp)())  # type: ignore[arg-type]
        assert [t for t, _ in clients] == [sealed]


# -- no production path ever proves the folder of a family shelf -------------------------------


@pytest.mark.req("EF-EXP-047", "ENF-SEC-016")
def test_the_daily_audit_alone_lets_a_family_shelf_be_linked_to_a_clean_komga() -> None:
    """The card: a family binding is allowed once the audit proves partitioning. In production
    `KomgaAuditor.admit` has no caller and the daily audit only replays the folders bound or
    proved before: a clean Komga (the teen reads 13+, the shelf is 13+) is refused forever with
    `folder_not_audited`. The red test of L22 only passes because it calls `admit` itself."""
    from shelfhold.jobs.daily_checks import komga_accounts_audit
    from tests.family_env import family_env
    from tests.fakes.komga import administrator, reader
    from tests.integration.test_komga_access_audit import LIBRARY, kit, try_bind

    with family_env(13) as env:
        fake, client, _auditor, events, target = kit(env)
        fake.users = [administrator(), reader("teen", [LIBRARY], age=13)]
        komga_accounts_audit(env.engine, env.clock, events, lambda: [(target, client)])()
        assert try_bind(env, target) is None
