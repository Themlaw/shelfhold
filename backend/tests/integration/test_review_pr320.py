"""#320 (M4 L08b) : relecture adverse contrôle parental de l'audit des comptes Komga.

Chaque PoC est rouge aujourd'hui pour la raison écrite dans `reason` ; `strict=True` signalera
le jour où le trou est fermé. Détail et remèdes : `docs/decisions/M4.md`, « #320 : contradicteur ».
Serveurs simulés, aucun réseau.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.komga_audit_state import proved_for
from shelfhold.export.domain.bindings import BindingError
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.komga.audit import KomgaAuditor
from shelfhold.infrastructure.schema import export
from tests.export_env import ExportEnv
from tests.fakes.komga import administrator, reader
from tests.family_env import add_adult_shelf, family_env
from tests.fan_out_env import bind
from tests.integration.test_komga_access_audit import LIBRARY, ROOT, admit, kit, library
from tests.kavita_env import run
from tests.kavita_export_env import wire

ADULT_LIBRARY = "0ADULTLIB1"
ADULT_ROOT = "/manga/AdultesK"


def paused(env: ExportEnv, shelf_id: str) -> str | None:
    with env.engine.connect() as connection:
        reason: str | None = connection.execute(
            sa.select(export.shelf.c.paused_reason).where(export.shelf.c.id == shelf_id)
        ).scalar_one()
    return reason


def labelled(name: str, libraries: list[str], **labels: list[str]) -> dict[str, Any]:
    """Un compte non administrateur restreint par libellés, sans restriction d'âge."""
    account = reader(name, libraries)
    account.update(labels)
    return account


@pytest.mark.req("EF-EXP-047")
@pytest.mark.xfail(
    strict=True,
    reason="M1 : un audit lent, lu avant un audit plus récent, écrit après lui : il rétablit la "
    "preuve et lève la pause que l'audit récent venait de poser",
)
def test_m1_a_stale_reading_never_lifts_a_newer_pause() -> None:
    with family_env(13) as env:
        fake, client, auditor, _events, target = kit(env)
        fake.users = [administrator(), reader("teen", [LIBRARY], age=13)]
        assert admit(env, auditor, client, target).proved
        bind(env, "Komga", target_id=target, link=False)
        stale = run(KomgaAuditor._read(client))  # l'audit A a lu Komga, puis traîne
        fake.users = [administrator(), reader("teen", [LIBRARY], age=6)]  # restriction abaissée
        newer = run(auditor.audit(client, target))  # l'audit B, plus récent, pose la pause
        assert newer.paused_shelves == [env.shelf_id]
        auditor._recorder.record(target, None, stale)  # A écrit enfin (= fin de `_run`)
        assert paused(env, env.shelf_id) == "child_exposure"
        with env.engine.connect() as connection:
            assert not proved_for(connection, target, ROOT, 13)


@pytest.mark.req("EF-EXP-047")
@pytest.mark.xfail(
    strict=True,
    reason="M2 : la preuve n'a pas de date de péremption ; sans contrôle quotidien branché, une "
    "preuve d'il y a un an vaut toujours `library`",
)
def test_m2_a_proof_expires_when_no_audit_renews_it() -> None:
    with family_env(13) as env:
        _fake, client, auditor, _events, target = kit(env)
        assert admit(env, auditor, client, target).proved
        env.clock.advance(days=365)
        with env.engine.connect() as connection:
            assert not proved_for(connection, target, ROOT, 13)


@pytest.mark.req("EF-EXP-047")
@pytest.mark.xfail(
    strict=True,
    reason="M3 : un compte restreint par libellés (labelsAllow / labelsExclude) sans âge est lu "
    "comme un membre ; il atteint la bibliothèque 18+ et l'audit la prouve",
)
@pytest.mark.parametrize("labels", [{"labelsExclude": ["adulte"]}, {"labelsAllow": ["enfants"]}])
def test_m3_a_label_restricted_account_is_a_child(labels: dict[str, list[str]]) -> None:
    with family_env(13) as env:
        fake, client, auditor, _events, target = kit(env)
        fake.users = [administrator(), labelled("kid", [LIBRARY], **labels)]
        report = admit(env, auditor, client, target, ceiling=None)  # l'étagère adulte
        assert not report.proved


@pytest.mark.req("EF-EXP-047")
@pytest.mark.xfail(
    strict=True,
    reason="M4 : un compte non administrateur sans aucune restriction, qui lit la bibliothèque "
    "familiale ET la bibliothèque 18+, n'empêche aucune preuve (« qui est un enfant »)",
)
def test_m4_an_unclassified_account_on_family_and_adult_libraries_proves_nothing() -> None:
    with family_env(13) as env:
        fake, client, auditor, _events, target = kit(
            env, libraries=[library(), library(ADULT_LIBRARY, ADULT_ROOT)]
        )
        assert admit(env, auditor, client, target).proved
        bind(env, "Komga", target_id=target, link=False)  # l'étagère familiale
        fake.users = [administrator(), reader("kid", [], shared_all=True)]  # oubli de restriction
        report = admit(env, auditor, client, target, ceiling=None, root=ADULT_ROOT)
        assert not report.proved


@pytest.mark.req("EF-EXP-047")
@pytest.mark.xfail(
    strict=True,
    reason="M5 : `target recheck` de Kavita (audit resume=True) lève la pause `child_exposure` "
    "posée par l'audit Komga sur une étagère liée aux deux",
)
def test_m5_kavita_never_lifts_a_pause_komga_posed() -> None:
    with family_env(13) as env:
        wired = wire(env)
        fake, client, auditor, _events, target = kit(env)
        fake.users = [administrator(), reader("teen", [LIBRARY], age=13)]
        assert admit(env, auditor, client, target).proved
        bind(env, "Komga", target_id=target, link=False)
        fake.users = [administrator(), reader("kid", [LIBRARY], age=6)]
        assert run(auditor.audit(client, target)).paused_shelves == [env.shelf_id]
        kavita = AccessAuditor(env.engine, env.clock, wired.events)
        run(kavita.audit(wired.client, env.target_id, resume=True))  # Kavita, lui, est propre
        assert paused(env, env.shelf_id) == "child_exposure"  # l'enfant de 6 ans lit toujours


@pytest.mark.req("EF-EXP-047")
@pytest.mark.xfail(
    strict=True,
    reason="M6 : une étagère 18+ se lie à un Komga dont le dernier audit a vu un enfant partagé "
    "sur toutes les bibliothèques : ni refus ni pause, rien n'est vérifié à la liaison",
)
def test_m6_an_adult_shelf_is_not_bound_where_a_child_reaches_every_library() -> None:
    with family_env(13) as env:
        adult_shelf, _ = add_adult_shelf(env)
        fake, client, auditor, _events, target = kit(
            env, libraries=[library(), library(ADULT_LIBRARY, ADULT_ROOT)]
        )
        fake.users = [administrator(), reader("kid", [], age=8, shared_all=True)]
        run(auditor.audit(client, target))  # Komga audité : l'enfant atteint tout
        try:
            bind(env, "AdultesK", target_id=target, shelf_id=adult_shelf, link=False)
        except BindingError:
            return  # refusée : correct
        assert paused(env, adult_shelf) == "child_exposure"
