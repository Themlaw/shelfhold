"""Relecture adverse de M4 L28 (#338) : le classement en masse peut abaisser une Œuvre déjà classée.

`to_rate` met en `rating_pending` toute Œuvre dont une Édition n'est pas classée, **y compris une
Œuvre déjà classée** qui reçoit une Édition nouvelle (EF-BIB-055). `rateable` n'écarte que
`suggestion_above` ; l'aperçu ne montre pas l'âge actuel ; l'empreinte ne porte que les
identifiants. Une commande de masse réécrit donc l'âge de l'Œuvre à l'âge du lot, plus bas que la
décision d'un administrateur : ce que voit un enfant s'élargit, sans écriture de contrôle parental.

Chaque cas est rouge sur la tête de la PR (`xfail(strict=True)`) et passera au vert avec la
correction (écarter les Œuvres déjà classées de la commande, ou les inclure dans l'empreinte et
refuser d'abaisser).
"""

import pytest

from shelfhold.infrastructure.db import write_transaction
from tests.integration.test_m4_l28_bulk import _Rig, rig  # noqa: F401  (fixture)
from tests.integration.test_schema_catalog import add_edition

pytestmark = pytest.mark.req("EF-BIB-023", "EF-BIB-036", "EF-BIB-055")


def _new_edition(rig: _Rig, work_id: str) -> None:  # noqa: F811
    """Une Édition arrive après le classement : `rated_at` nul, sur aucune étagère."""
    with write_transaction(rig.engine) as connection:
        add_edition(connection, work_id, lang="en", shelf_id=None)


def test_a_rated_work_with_a_new_edition_is_not_lowered_by_a_bulk_order(
    rig: _Rig,  # noqa: F811
) -> None:
    """L'administrateur a classé l'Œuvre 18+ ; une Édition arrive ; « tout ce qui est affiché »
    à 13+ ne doit pas ramener l'Œuvre (et toutes ses Éditions) à 13+."""
    adult, fresh = rig.waiting("Adulte", "Neuve")
    rig.rate_elsewhere(adult, "18+")
    _new_edition(rig, adult)
    preview = rig.preview(work_ids=[adult, fresh])
    job_id = rig.queue(preview)
    rig.run()
    assert rig.job(job_id).status == "succeeded"
    assert rig.age(fresh) == 13
    assert rig.age(adult) == 18  # rouge : 13, un enfant 13+ voit désormais l'Œuvre adulte


def test_a_rating_given_between_preview_and_job_makes_the_order_stale(
    rig: _Rig,  # noqa: F811
) -> None:
    """Le « tout ou rien » promis : une Œuvre choisie classée entre l'aperçu et le travail arrête
    la commande. Il ne tient plus si une Édition arrive juste après ce classement : l'Œuvre
    revient dans la liste, l'empreinte (identifiants seuls) est la même, et le travail écrase
    la décision 18+ par l'âge du lot."""
    a, b = rig.waiting("A", "B")
    preview = rig.preview(work_ids=[a, b])
    rig.rate_elsewhere(b, "18+")  # un autre administrateur tranche pendant que l'aperçu attend
    _new_edition(rig, b)  # puis une Édition de B arrive (suivi, téléchargement)
    job_id = rig.queue(preview)
    rig.run()
    job = rig.job(job_id)
    assert job.error_code == "bulk.stale"  # rouge : `succeeded`
    assert rig.age(b) == 18 and rig.age(a) is None
