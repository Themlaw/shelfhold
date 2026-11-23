"""Contre-relecture de L22 : un dossier en attente sans bibliothèque Komga retire la preuve de
tous les autres.

`KomgaAuditRecorder._pending` ajoute à l'audit du jour les arbres de **toutes** les étagères sous
18+ que le path map du Komga couvre. Leurs écarts portent `shelf_id=None`, comme ceux des autres
dossiers en attente : un seul écart (ici une étagère familiale servie par Kavita seul, dont aucune
bibliothèque Komga ne tient le dossier) met `None` dans `bad`, et aucun dossier en attente n'est
prouvé. L'étagère familiale propre reste refusée pour toujours (`folder_not_audited`).
"""

from __future__ import annotations

import pytest

from shelfhold.jobs.daily_checks import komga_accounts_audit
from tests.export_env import add_second_shelf
from tests.family_env import family_env
from tests.fakes.komga import administrator, reader
from tests.integration.test_komga_access_audit import LIBRARY, kit, try_bind


@pytest.mark.req("EF-EXP-047", "ENF-SEC-016")
def test_a_family_shelf_komga_does_not_map_never_blocks_the_proof_of_another() -> None:
    with family_env(13) as env:
        add_second_shelf(env, max_age_rating=13)  # a family shelf for Kavita only: Kavita2
        fake, client, _auditor, events, target = kit(env)  # one Komga library: /manga/Kavita
        fake.users = [administrator(), reader("teen", [LIBRARY], age=13)]
        komga_accounts_audit(env.engine, env.clock, events, lambda: [(target, client)])()
        assert try_bind(env, target) is None  # /manga/Kavita is clean: the link is accepted
