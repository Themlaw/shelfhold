"""#331, relecture adverse (contradicteur) de M4 L18b : contrôle parental et intégrité de Kavita.

Chaque PoC est rouge aujourd'hui (`xfail(strict=True)`) et décrit un trou réel ; il doit passer au
vert quand le trou est fermé. Même décor que `test_m4_l18b_kavita_collections.py` : base et
registre de production, Kavita simulé.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from tests.export_env import export_env
from tests.integration.test_m4_l18b_kavita_collections import (
    SERVICE,
    Scene,
    add_user,
    make,
    seen,
    state,
    sync,
)
from tests.kavita_export_env import wire


@pytest.mark.req("EF-EXP-025", "ENF-SEC-010")
@pytest.mark.xfail(
    strict=True,
    reason="#331 M1 : après une restauration, un objet promu n'est plus jamais retiré",
)
def test_after_a_restore_a_collection_that_became_forbidden_is_still_withdrawn() -> None:
    """`collection_remote` est `_REBUILT` par la sauvegarde : restaurée, elle est vide. Le titre
    est alors « pris » par l'objet que Shelfhold avait fait : rien n'est plus poussé, **ni
    retiré**. La collection, promue, reste visible même quand ses séries ne sont plus dans des
    bibliothèques de même audience : l'enfant lit son nom."""
    with export_env(max_age_rating=None) as env:
        w = wire(env)
        s = Scene(w)
        with write_transaction(env.engine) as c:  # les deux étagères ouvertes aux mêmes profils
            c.execute(sa.update(export.shelf).values(max_age_rating=None, allowed_profiles=[]))
        make(env, "Tout, tome 9", [s.kid_one, s.adult_one])
        sync(w)
        assert [(t[1], t[2]) for t in state(w)] == [("Tout, tome 9", True)]
        with write_transaction(env.engine) as c:  # la restauration d'une sauvegarde
            c.execute(sa.delete(catalog.collection_remote))
        with write_transaction(env.engine) as c:  # l'étagère adulte se ferme
            c.execute(
                sa.update(export.shelf)
                .where(export.shelf.c.id == s.adult_shelf)
                .values(allowed_profiles=[new_ulid()], max_age_rating=99)
            )
        sync(w)
        sync(w)
        assert "Tout, tome 9" not in str(seen(w, "kid"))


@pytest.mark.req("EF-EXP-025", "ENF-SEC-010")
@pytest.mark.xfail(
    strict=True,
    reason="#331 M2 : la liste d'un adulte, approuvée, est promue entre deux audiences",
)
def test_an_approved_adult_list_across_two_audiences_never_shows_its_name_to_a_child() -> None:
    """La règle du foyer (« jamais entre bibliothèques d'audiences différentes : un nom peut dire
    « … tome 9 ») ne vaut pas pour la liste d'un profil : seule compte l'ouverture au
    **propriétaire**. Un adulte (sans plafond) a tout d'ouvert : sa liste, qui mêle la bibliothèque
    des enfants et celle des adultes, est promue, et Kavita la montre à l'enfant, amputée, sous son
    nom."""
    with export_env(max_age_rating=13) as env:
        w = wire(env)
        s = Scene(w)
        grown = add_user(env, "grown")  # un membre adulte, sans plafond
        make(
            env, "Grown one, tome 9", [s.kid_one, s.adult_one],
            kind="reading_list", owner=grown, approved=True,
        )  # fmt: skip
        sync(w)
        assert "Grown one" not in str(seen(w, "kid"))


@pytest.mark.req("EF-EXP-025", "ENF-SEC-010")
@pytest.mark.xfail(
    strict=True,
    reason="#331 m1 : un objet laissé à une personne est repris et promu avec ce qu'elle y a mis",
)
def test_an_object_left_to_a_person_is_not_promoted_again_with_what_she_put_there() -> None:
    """`_withdraw` laisse en place, non promu, un objet qui contient autre chose (« n'est plus à
    Shelfhold »), mais garde sa ligne et son `remote_id`. Quand la collection redevient poussable,
    `_reconcile` reprend cet objet et le **promeut** avec la série posée à la main, sans regarder
    ce qu'il contient d'autre : ici une série adulte, et la collection croise deux audiences."""
    with export_env(max_age_rating=13) as env:
        w = wire(env)
        s = Scene(w)
        coll = make(env, "Cosy reads", [s.kid_one])
        sync(w)
        ours = next(c for c in w.fake.shelves.collections.values() if c["owner"] == SERVICE)
        ours["series"].append(s.a_one)  # une personne y ajoute une série adulte, à la main
        items = catalog.collection_item
        with write_transaction(env.engine) as c:  # Shelfhold vide la collection : retrait
            c.execute(sa.delete(items).where(items.c.collection_id == coll))
        sync(w)
        assert ours["promoted"] is False and ours["series"] == [s.a_one]
        with write_transaction(env.engine) as c:  # puis la remplit de nouveau
            c.execute(
                sa.insert(items).values(
                    id=new_ulid(), collection_id=coll, position=0, work_id=s.kid_one.work_id
                )
            )
        sync(w)
        assert not (ours["promoted"] and s.a_one in ours["series"])
