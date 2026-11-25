"""The author of a Work in the list of Works (`GET /api/v1/works`): the first of its contributors,
named on the Work, else on the Edition the reader may see. An Edition she may not see lends no
name, exactly as it lends no cover (EF-USR-015)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import catalog
from tests.auth_env import AuthEnv, auth_env
from tests.visibility_env import Seed, add_profile, seed_catalogue

pytestmark = pytest.mark.req("EF-USR-015", "ENF-SEC-010")

FETCHED = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)


def _contributors(connection: sa.Connection, entity: str, entity_id: str, value: object) -> None:
    connection.execute(
        sa.insert(catalog.metadata_field).values(
            entity_type=entity, entity_id=entity_id, field="contributors", lang="und",
            value=json.dumps(value), provider="source-a", provenance="provider", fetched_at=FETCHED,
        )
    )  # fmt: skip


@pytest.fixture
def world(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> Iterator[tuple[AuthEnv, Seed, TestClient]]:
    with auth_env(monkeypatch, settings) as env:
        seed = seed_catalogue(env)
        kid = add_profile(env, "kid", "child", "10.0.0.2", max_age_rating=10)
        assert env.login().status_code == 200  # the admin
        with seed.engine.begin() as connection:
            _contributors(
                connection, "work", seed.works["ok"], ["Jules Verne", "Alphonse de Neuville"]
            )
            _contributors(connection, "edition", seed.editions["ok"], ["Someone Else"])
            _contributors(connection, "edition", seed.editions["twelve"], [{"name": "Hokusai"}])
            _contributors(connection, "edition", seed.editions["above"], ["Hidden Writer"])
            _contributors(connection, "work", seed.works["mixed"], [])  # an empty list names no one
        yield env, seed, kid


def _authors(client: TestClient) -> dict[str, str | None]:
    answer = client.get("/api/v1/works", params={"limit": 200})
    assert answer.status_code == 200
    return {item["work_id"]: item["author"] for item in answer.json()["items"]}


def test_the_list_names_the_first_contributor_of_the_work_then_of_its_edition(
    world: tuple[AuthEnv, Seed, TestClient],
) -> None:
    env, seed, _ = world
    authors = _authors(env.client)
    assert authors[seed.works["ok"]] == "Jules Verne"  # the Work's own, before its Edition's
    assert authors[seed.works["twelve"]] == "Hokusai"  # a contributor given as an object
    assert authors[seed.works["mixed"]] is None
    assert authors[seed.works["above"]] == "Hidden Writer"  # the admin sees every Edition


def test_a_hidden_work_lends_no_name_to_a_child(world: tuple[AuthEnv, Seed, TestClient]) -> None:
    _, seed, kid = world
    answer = kid.get("/api/v1/works", params={"limit": 200})
    assert answer.status_code == 200
    assert "Hidden Writer" not in answer.text
    assert seed.works["above"] not in _authors(kid)
