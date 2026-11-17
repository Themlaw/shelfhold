"""#327 (M4 L27), relecture adverse : les trous trouvés, un test rouge chacun (xfail strict).

Chaque test décrit ce que la migration devrait garantir et échoue tant que le trou est ouvert.
Le jour où il est fermé, le test passe et l'xfail strict le signale : on retire alors la marque.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import Override, StaleMigrationError, number_map_of
from shelfhold.catalog.public import ListedUnit, place_key
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import acquisition
from shelfhold.ops.errors import CodedRefusal
from shelfhold.ops.series_migration import SeriesMigrationOps
from tests.integration import test_m4_l27_source_migration as l27
from tests.integration.test_m4_l27_source_migration import NOW, Moved, admin, listing
from tests.integration.test_schema_catalog import add_unit

pytestmark = pytest.mark.req("EF-BIB-014")
moved = l27.moved  # la fixture du lot, réutilisée telle quelle


def test_a_later_release_never_lands_where_a_corrected_line_already_lands(moved: Moved) -> None:
    """La nouvelle source a sauté un numéro : son 7 est le 8 de la bibliothèque, et l'admin le
    corrige à la main. Le 8 qu'elle publie ensuite (le 9 de la bibliothèque) est lu par le décalage
    global (0) : il tombe sur le 8, la place de la ligne corrigée. Deux numéros de la source sur
    une place : le suivi tient le 8 pour déjà demandé et ne demande jamais le 9."""
    proposal = moved.propose(listing(1, 7))
    assert proposal.offset == 0
    validated = moved.validate(proposal, (Override("7", "8"),))
    done = moved.apply(validated)
    with moved.engine.connect() as c:
        numbers = number_map_of(c, done.binding_id)
    assert numbers is not None
    corrected = numbers.translate("7", 6)
    later = numbers.translate("8", 7)  # publié après la validation
    assert place_key(*corrected) != place_key(*later)


def _unnumbered(moved: Moved, order: int) -> str:
    with write_transaction(moved.engine) as c:
        return add_unit(
            c, moved.edition, "", number_raw="Special", number_norm="", number_sort=None,
            track="unnumbered", kind="chapter", source_order=order,
        )  # fmt: skip


def test_an_unnumbered_release_of_the_new_source_is_never_taken_for_a_held_one(
    moved: Moved,
) -> None:
    """L'ancienne source rangeait un hors-série sans numéro au rang 6 ; la nouvelle liste un autre
    hors-série sans numéro au même rang. Le rang d'une liste ne dit rien d'une autre source :
    la ligne doit rester « new » (ou exiger un choix de l'admin), jamais « same_number » sur
    l'Unit tenue, sinon ce hors-série n'est jamais téléchargé."""
    held_extra = _unnumbered(moved, 6)
    listed = (*listing(1, 5), ListedUnit(None, 6))
    proposal = moved.propose(listed)
    extra = next(r for r in proposal.rows if r.number is None)
    # l'admin ne peut pas nommer cette ligne : une dérogation se fait par numéro
    assert extra.unit_id != held_extra


def test_apply_refuses_a_table_whose_source_listing_changed(moved: Moved) -> None:
    """M4.md promet « une source qui a changé entre-temps donne source_migration.stale ». La
    source listait 11..17 (le 1..7 de la bibliothèque) quand la table a été proposée et validée ;
    elle renumérote ensuite en 21..27. L'application ne relit rien : la table s'applique, et le
    décalage -10 lit désormais le 23 de la source comme le 13 de la bibliothèque."""
    now_listed: dict[str, Any] = {"listed": listing(11, 17)}
    ref = SimpleNamespace(source_key="suwayomi:7002", remote_id="/new-source")
    comp = SimpleNamespace(engine=moved.engine, clock=SimpleNamespace(now=lambda: NOW))
    ops = SeriesMigrationOps(comp, listing=lambda _key, _remote: (ref, now_listed["listed"]))  # type: ignore[arg-type]
    shown = ops.propose(
        moved.edition, source_key="suwayomi:7002", remote_id="/new-source",
        replaces=moved.old_binding, principal=admin(),
    )  # fmt: skip
    validated = ops.validate(
        moved.edition, shown["id"], digest=shown["digest"], overrides=[], principal=admin()
    )
    now_listed["listed"] = listing(21, 27)  # la source a renuméroté entre-temps
    with pytest.raises((StaleMigrationError, CodedRefusal)):
        ops.apply(moved.edition, shown["id"], digest=validated["digest"], principal=admin())
    with moved.engine.connect() as c:
        bound = c.execute(
            sa.text("SELECT count(*) FROM source_binding WHERE active AND edition_id = :e"),
            {"e": moved.edition},
        ).scalar()
    assert bound == 0


# -- les correctifs de la relecture adverse : ce que les PoC ne disent pas -----------------------


def _applied(moved: Moved, listed: tuple[ListedUnit, ...], **extra: Any) -> Any:
    return moved.apply(moved.validate(moved.propose(listed, **extra)))


def test_a_table_with_a_line_moved_by_hand_holds_a_new_number_aside(
    moved: Moved,
) -> None:
    """Un décalage qui n'est pas celui de toute la table ne dit rien de ce que la source publiera :
    un numéro que la table ne liste pas n'est placé nulle part, il attend l'admin."""
    proposal = moved.propose(listing(1, 5))
    assert proposal.offset == 0
    done = moved.apply(moved.validate(proposal, (Override("5", "9"),)))  # one line moved by hand
    with moved.engine.connect() as c:
        numbers = number_map_of(c, done.binding_id)
    assert numbers is not None
    assert numbers.held_aside("6", 5)
    assert numbers.translate("6", 5)[0] is None
    assert not numbers.held_aside("2", 1)  # a line of the table is read as the table says


def test_a_number_that_would_land_on_a_taken_place_is_held_aside(moved: Moved) -> None:
    done = _applied(moved, listing(11, 17))
    with moved.engine.connect() as c:
        numbers = number_map_of(c, done.binding_id)
    assert numbers is not None
    assert not numbers.held_aside("18", 7)  # the library's 8: free
    assert numbers.translate("18", 7) == ("8", 7)
    assert numbers.held_aside("1", 0)  # 1 - 10 is no number: and 1 is the library's own: taken


def test_a_release_without_a_number_is_paired_by_the_admin_by_its_place(moved: Moved) -> None:
    held_extra = _unnumbered(moved, 6)
    proposal = moved.propose((*listing(1, 5), ListedUnit(None, 6)))
    extra = next(r for r in proposal.rows if r.number is None)
    assert extra.basis == "new" and extra.unit_id is None
    validated = moved.validate(proposal, (Override("", held_extra, place=extra.place),))
    paired = next(r for r in validated.rows if r.place == extra.place)
    assert paired.unit_id == held_extra and paired.basis == "manual"
    assert moved.apply(validated).proposal.to_fetch == ()
    with pytest.raises(Exception):  # noqa: B017 (a line that does not exist)
        moved.validate(moved.propose(listing(1, 5)), (Override("", held_extra, place="nope"),))


def test_applying_one_table_closes_the_others_and_a_second_cannot_follow(moved: Moved) -> None:
    first = moved.propose(listing(1, 5))
    second = moved.propose(listing(1, 6), source_key="suwayomi:7003", remote_id="/another")
    moved.apply(moved.validate(first))
    with moved.engine.connect() as c:
        states = dict(c.execute(sa.text("SELECT id, state FROM source_migration")).all())
    assert states[first.id] == "applied" and states[second.id] == "cancelled"
    with pytest.raises(Exception):  # noqa: B017 (not validated: it is closed)
        moved.apply(second)
    assert moved.bindings() == 2  # the old one and the one migrated


def test_the_migrated_binding_keeps_the_rank_of_the_one_it_replaces(moved: Moved) -> None:
    with write_transaction(moved.engine) as c:
        c.execute(
            sa.update(acquisition.source_binding)
            .where(acquisition.source_binding.c.id == moved.old_binding)
            .values(priority=3)
        )
    done = _applied(moved, listing(1, 5))
    with moved.engine.connect() as c:
        rank = c.execute(
            sa.select(acquisition.source_binding.c.priority).where(
                acquisition.source_binding.c.id == done.binding_id
            )
        ).scalar()
    assert rank == 3  # a better rank would ask again every held chapter as an upgrade
