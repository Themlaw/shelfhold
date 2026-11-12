"""M3 L09 (EF-BIB-055, ENF-SEC-010): "Ajouter directement" never sends an Edition nobody rated to
a family shelf, whoever the adult is.

The table of truth gains the row "member -> add directly -> family shelf": an adult without the
right to classify is refused on a family shelf (D14: she asks instead), an Edition nobody rated is
refused to every role (it waits in "À classer"), and a child, a key or a token never reaches the
route. Everything goes through the real application, and the database is read back.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.identity.public import local_auth
from tests.auth_env import CSRF_HEADER, AuthEnv, auth_env
from tests.visibility_env import Seed, add_profile, seed_catalogue

ULID = "01HZZZZZZZZZZZZZZZZZZZZZZZ"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[tuple[AuthEnv, Seed]]:
    with auth_env(monkeypatch, settings) as env:
        seed = seed_catalogue(env)
        assert env.login().status_code == 200
        yield env, seed
        seed.engine.dispose()


def _add(env: AuthEnv, client: TestClient, edition_id: str, shelf_id: str | None) -> Any:
    return client.post(
        f"/api/v1/editions/{edition_id}/add-directly",
        json={"shelf_id": shelf_id},
        headers={CSRF_HEADER: env.csrf(client)},
    )


def _shelf_of(seed: Seed, edition_id: str) -> str | None:
    with seed.engine.connect() as connection:
        row = connection.execute(
            sa.text("SELECT shelf_id FROM edition WHERE id = :e"), {"e": edition_id}
        ).scalar()
    return None if row is None else str(row)


def _audit(seed: Seed, action: str) -> list[str]:
    with seed.engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT target FROM audit WHERE action = :a ORDER BY id"), {"a": action}
        ).all()
    return [str(r[0]) for r in rows]


@pytest.mark.req("EF-BIB-055", "ENF-SEC-010")
def test_adult_add_directly_blocked_for_unrated_edition(api: tuple[AuthEnv, Seed]) -> None:
    """The acceptance of EF-BIB-055 (M3): a member without the right to classify chooses
    "Ajouter directement" towards a family shelf for an Edition nobody rated, and it does not go."""
    env, seed = api
    mum = add_profile(env, "mum", "member", "10.0.0.3")  # no right to classify
    family, edition = seed.shelves["family"], seed.editions["mixed_new"]  # new, unrated Edition
    assert _shelf_of(seed, edition) is None
    answer = _add(env, mum, edition, family)
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"filed": False, "reason": "unrated_edition"}
    assert _shelf_of(seed, edition) is None  # it did not enter: it waits in "À classer"
    assert _audit(seed, "edition.add_directly_refused")  # and the attempt is on record


@pytest.mark.req("EF-BIB-055", "ENF-SEC-010")
def test_add_directly_truth_table_row(api: tuple[AuthEnv, Seed]) -> None:
    """role x right to classify x Edition x shelf: only what the row allows goes through."""
    env, seed = api
    family, adult = seed.shelves["family"], seed.shelves["adult"]
    plain = add_profile(env, "plain", "member", "10.0.0.3")
    rater = add_profile(env, "rater", "member", "10.0.0.4", can_rate=True)
    rated, unrated = seed.editions["twelve"], seed.editions["mixed_new"]  # 12+ on a 13+ shelf
    # no right to classify: a family shelf is refused, rated or not (she asks, D14)
    assert _add(env, plain, rated, family).json() == {
        "filed": False, "reason": "rate_right_required",
    }  # fmt: skip
    assert _add(env, plain, unrated, family).json()["reason"] == "unrated_edition"
    # with the right to classify: an Edition nobody rated is still refused, a rated one is filed
    assert _add(env, rater, unrated, family).json() == {"filed": False, "reason": "unrated_edition"}
    assert _shelf_of(seed, unrated) is None and _shelf_of(seed, rated) is None
    assert _add(env, rater, rated, family).json() == {"filed": True, "reason": None}
    assert _shelf_of(seed, rated) == family
    # an adult shelf takes anything, for every adult
    assert _add(env, plain, unrated, adult).json() == {"filed": True, "reason": None}
    assert _shelf_of(seed, unrated) == adult
    assert _audit(seed, "edition.add_directly") == [
        f"edition:{rated};shelf:{family}", f"edition:{unrated};shelf:{adult}",
    ]  # fmt: skip


@pytest.mark.req("EF-BIB-055", "ENF-SEC-010")
def test_admin_add_directly_goes_through_the_same_admission(api: tuple[AuthEnv, Seed]) -> None:
    env, seed = api
    family = seed.shelves["family"]
    assert _add(env, env.client, seed.editions["mixed_new"], family).json()["reason"] == (
        "unrated_edition"
    )
    assert _add(env, env.client, seed.editions["above"], family).json()["reason"] == (
        "above_ceiling"
    )  # a 16+ Work does not fit a 13+ shelf
    assert _shelf_of(seed, seed.editions["mixed_new"]) is None


@pytest.mark.req("EF-BIB-055", "ENF-SEC-010")
def test_child_key_and_stranger_never_reach_add_directly(api: tuple[AuthEnv, Seed]) -> None:
    env, seed = api
    kid = add_profile(env, "kid", "child", "10.0.0.2", max_age_rating=10)
    edition, adult = seed.editions["mixed_new"], seed.shelves["adult"]
    assert _add(env, kid, edition, adult).status_code == 403  # a child never adds directly
    auth = local_auth(seed.engine, env.settings)
    add_profile(env, "mum", "member", "10.0.0.3")
    key = auth.create_api_key(name="k", username="mum", read_only=False).value
    by_key = env.other_client("10.0.0.9").post(
        f"/api/v1/editions/{edition}/add-directly",
        json={"shelf_id": adult},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert by_key.status_code == 403  # a key is a program, not a person who chose
    anonymous = env.other_client("10.0.0.10").post(
        f"/api/v1/editions/{edition}/add-directly", json={"shelf_id": adult}
    )
    assert anonymous.status_code in (401, 403)
    assert _shelf_of(seed, edition) is None


@pytest.mark.req("EF-BIB-055", "ENF-SEC-010")
def test_add_directly_attacks_refused_without_a_side_effect(api: tuple[AuthEnv, Seed]) -> None:
    env, seed = api
    plain = add_profile(env, "plain", "member", "10.0.0.3")
    family = seed.shelves["family"]
    for edition, shelf in (
        (ULID, family),  # an Edition that does not exist
        (seed.editions["ok"], ULID),  # a shelf that does not exist
    ):
        assert _add(env, plain, edition, shelf).status_code == 404
    assert _add(env, plain, seed.editions["mixed_new"], None).status_code == 422  # a shelf is named
    bad = plain.post(
        f"/api/v1/editions/{seed.editions['ok']}/add-directly",
        json={"shelf_id": family, "age": 0, "rated": True},  # nothing but the shelf is accepted
        headers={CSRF_HEADER: env.csrf(plain)},
    )
    assert bad.status_code == 422
    # the admin's own route for choosing a family shelf stays the administrator's
    forced = plain.post(
        f"/api/v1/editions/{seed.editions['mixed_new']}/shelf",
        json={"shelf_id": family},
        headers={CSRF_HEADER: env.csrf(plain)},
    )
    assert forced.status_code == 403
    assert _shelf_of(seed, seed.editions["mixed_new"]) is None
