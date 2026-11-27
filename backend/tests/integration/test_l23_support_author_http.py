"""M4 L23 (EF-BIB-032, ENF-SEC-010): the attacks on « Soutenir l'auteur ».

The read is every profile's, through the one filter of the catalogue: a child gets the links only
of an Edition she may see, and an Edition she may not see answers exactly like one that does not
exist. A member sees the links and is told nothing about bindings. The proposal to pause a
community binding is the admin's alone, and so is the act; a binding that was not proposed for
the Edition (the identifier of someone else's) is refused. Real application, real sessions.
"""

# ruff: noqa: F811  (the fixtures `api` and `world` are those of the child-surface suite)
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.schema import acquisition, catalog
from tests.auth_env import CSRF_HEADER
from tests.integration.test_child_surfaces import (  # noqa: F401  (fixtures)
    HIDDEN_FROM_CEILING_10,
    MISSING,
    World,
    api,
    world,
)
from tests.integration.test_schema_catalog import add_binding, add_source_item
from tests.visibility_env import add_profile

NOW = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
URL = "https://publisher.example.org/read-me"
HIDDEN_URL = "https://hidden.example.org/never-shown-to-a-child"


def _read(world: World, client: Any, edition: str) -> Any:
    return client.get(f"/api/v1/editions/{edition}/support-author")


def _link(world: World, edition: str, url: str, provider: str = "anilist") -> None:
    with world.seed.engine.begin() as c:
        c.execute(
            sa.insert(catalog.external_link).values(
                id=_ulid(), entity_type="edition", entity_id=edition, url=url,
                provider=provider, fetched_at=NOW,
            )
        )  # fmt: skip


def _ulid() -> str:
    from shelfhold.infrastructure.ids import new_ulid

    return new_ulid()


def _bindable(world: World) -> dict[str, str]:
    """The visible Work gets an official Edition in the language of the one the household follows
    (a community binding on it), and the Work above the child's ceiling gets the same."""
    out: dict[str, str] = {}
    with world.seed.engine.begin() as c:
        for key in ("ok", "above"):
            community = world.seed.editions[key]
            work = c.execute(
                sa.select(catalog.edition.c.work_id).where(catalog.edition.c.id == community)
            ).scalar_one()
            row = (
                c.execute(sa.select(catalog.edition).where(catalog.edition.c.id == community))
                .mappings()
                .one()
            )
            official = _ulid()
            c.execute(
                sa.insert(catalog.edition).values(
                    id=official, work_id=work, lang=row["lang"],
                    translation_type="official",
                    media="images", is_official=True, publisher="Official Pub",
                    rated_at=row["rated_at"],
                    age_rating=row["age_rating"],
                )
            )  # fmt: skip
            out[f"{key}_official"] = official
            out[f"{key}_binding"] = add_binding(
                c, work, community, add_source_item(c, remote_id=f"s-{key}")
            )
    return out


@pytest.mark.req("EF-BIB-032", "ENF-SEC-010")
def test_child_gets_the_links_of_a_visible_edition_and_nothing_of_a_hidden_one(
    world: World,
) -> None:
    ids = _bindable(world)
    visible, hidden = world.seed.editions["ok"], world.seed.editions["above"]
    _link(world, visible, URL)
    _link(world, hidden, HIDDEN_URL)
    answer = _read(world, world.kid, visible)
    assert answer.status_code == 200
    body = answer.json()
    assert [(link["url"], link["provider"]) for link in body["links"]] == [(URL, "anilist")]
    assert body["links"][0]["fetched_at"]  # the provenance travels with the link
    assert body["pause_suggestions"] == []  # a child is told nothing about a binding
    assert ids["ok_binding"] not in answer.text
    # an Edition above her ceiling, unrated, absent: the same 404, no link, no id
    absent = _read(world, world.kid, MISSING)
    assert absent.status_code == 404
    for key in ("above", "adult_edition", "misfiled", "mixed_new"):
        refused = _read(world, world.kid, world.seed.editions[key])
        assert (refused.status_code, refused.json()["type"]) == (404, absent.json()["type"]), key
        assert HIDDEN_URL not in refused.text and ids["above_binding"] not in refused.text
        assert refused.json().get("detail") == absent.json().get("detail"), key
    assert HIDDEN_FROM_CEILING_10  # (the world's own markers are checked below)
    world.clean(answer.text)


@pytest.mark.req("EF-BIB-032", "ENF-SEC-010")
def test_member_sees_links_and_no_proposal_and_cannot_pause(world: World) -> None:
    ids = _bindable(world)
    edition = world.seed.editions["ok"]
    _link(world, edition, URL)
    member = add_profile(world.env, "mem", "member", "10.0.0.3")
    answer = _read(world, member, edition)
    assert answer.status_code == 200
    assert [link["url"] for link in answer.json()["links"]] == [URL]
    assert answer.json()["pause_suggestions"] == []
    refused = member.post(
        f"/api/v1/editions/{edition}/bindings/{ids['ok_binding']}/pause",
        headers={CSRF_HEADER: world.env.csrf(member)},
    )
    assert refused.status_code == 403
    assert _active(world, ids["ok_binding"]) is True


@pytest.mark.req("EF-BIB-032", "ENF-SEC-010")
def test_child_cannot_pause_and_a_key_reads_as_its_profile(world: World) -> None:
    from shelfhold.identity.public import local_auth

    ids = _bindable(world)
    edition = world.seed.editions["ok"]
    _link(world, world.seed.editions["above"], HIDDEN_URL)
    refused = world.kid.post(
        f"/api/v1/editions/{edition}/bindings/{ids['ok_binding']}/pause",
        headers={CSRF_HEADER: world.env.csrf(world.kid)},
    )
    assert refused.status_code == 403
    assert _active(world, ids["ok_binding"]) is True
    key = (
        local_auth(world.seed.engine, world.env.settings)
        .create_api_key(name="kidkey", username="kid", read_only=True)
        .value
    )
    anonymous = world.env.other_client("10.0.0.53")
    auth = {"Authorization": f"Bearer {key}"}
    own = anonymous.get(f"/api/v1/editions/{edition}/support-author", headers=auth)
    assert own.status_code == 200 and own.json()["pause_suggestions"] == []
    hidden = anonymous.get(
        f"/api/v1/editions/{world.seed.editions['above']}/support-author", headers=auth
    )
    assert hidden.status_code == 404 and HIDDEN_URL not in hidden.text
    # a key never pauses a binding
    paused = anonymous.post(
        f"/api/v1/editions/{edition}/bindings/{ids['ok_binding']}/pause", headers=auth
    )
    assert paused.status_code in (401, 403)
    assert _active(world, ids["ok_binding"]) is True


@pytest.mark.req("EF-BIB-032")
def test_anonymous_gets_nothing(world: World) -> None:
    edition = world.seed.editions["ok"]
    _link(world, edition, URL)
    anonymous = world.env.other_client("10.0.0.54")
    assert anonymous.get(f"/api/v1/editions/{edition}/support-author").status_code == 401
    assert anonymous.post(f"/api/v1/editions/{edition}/bindings/x/pause").status_code in (401, 403)


@pytest.mark.req("EF-BIB-032")
def test_admin_is_proposed_the_pause_then_decides(world: World) -> None:
    ids = _bindable(world)
    edition = world.seed.editions["ok"]
    admin = world.env.client
    proposal = _read(world, admin, edition)
    assert proposal.status_code == 200
    assert proposal.json()["pause_suggestions"] == [ids["ok_binding"]]
    assert _active(world, ids["ok_binding"]) is True  # a proposal, never automatic
    # the CSRF of a session applies
    assert (
        admin.post(
            f"/api/v1/editions/{edition}/bindings/{ids['ok_binding']}/pause",
            headers={CSRF_HEADER: "wrong"},
        ).status_code
        == 403
    )
    assert _active(world, ids["ok_binding"]) is True
    done = admin.post(
        f"/api/v1/editions/{edition}/bindings/{ids['ok_binding']}/pause",
        headers={CSRF_HEADER: world.env.csrf(admin)},
    )
    assert done.status_code == 200 and done.json()["paused"] is True
    assert _active(world, ids["ok_binding"]) is False
    assert _read(world, admin, edition).json()["pause_suggestions"] == []


@pytest.mark.req("EF-BIB-032", "ENF-SEC-010")
def test_admin_cannot_pause_a_binding_of_another_edition(world: World) -> None:
    ids = _bindable(world)
    admin = world.env.client
    headers = {CSRF_HEADER: world.env.csrf(admin)}
    foreign = admin.post(
        f"/api/v1/editions/{world.seed.editions['ok']}/bindings/{ids['above_binding']}/pause",
        headers=headers,
    )
    assert foreign.status_code == 404  # the IDOR: a binding that was not proposed for this one
    missing = admin.post(
        f"/api/v1/editions/{world.seed.editions['ok']}/bindings/{MISSING}/pause", headers=headers
    )
    assert missing.status_code == 404
    nowhere = admin.post(
        f"/api/v1/editions/{MISSING}/bindings/{ids['ok_binding']}/pause", headers=headers
    )
    assert nowhere.status_code == 404
    assert (
        _active(world, ids["above_binding"]) is True and _active(world, ids["ok_binding"]) is True
    )


def _active(world: World, binding: str) -> bool:
    with world.seed.engine.connect() as c:
        return bool(
            c.execute(
                sa.select(acquisition.source_binding.c.active).where(
                    acquisition.source_binding.c.id == binding
                )
            ).scalar_one()
        )


@pytest.mark.req("EF-BIB-032")
def test_a_read_key_of_the_admin_is_proposed_no_pause(world: World) -> None:
    """The proposal is the admin's power (SEC-1): a read key of hers reads the links as her
    profile, and is told nothing about bindings."""
    from shelfhold.identity.public import local_auth

    _bindable(world)
    edition = world.seed.editions["ok"]
    key = local_auth(world.seed.engine, world.env.settings).create_api_key(
        name="adminread", read_only=True
    ).value
    keyed = world.env.other_client("10.0.0.55")
    got = keyed.get(
        f"/api/v1/editions/{edition}/support-author", headers={"Authorization": f"Bearer {key}"}
    )
    assert got.status_code == 200 and got.json()["pause_suggestions"] == []
