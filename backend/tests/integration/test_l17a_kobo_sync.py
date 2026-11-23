"""M6 L17a: the Kobo sync emulation, minimal surface (EF-INT-012, D20, Q21 case (b) of D-P5).

The shapes are the ones the device speaks, written by hand from the form of the protocol (it is
not documented by the manufacturer): `GET /kobo/<token>/v1/initialization` answers the resources of
our own server and nothing of the manufacturer's, `POST v1/auth/device` is a dummy that accepts
and stores nothing, `GET v1/library/sync` answers entitlements, tags and reading states page by
page (`x-kobo-synctoken` to continue, `X-Kobo-sync: continue` while a page follows), and every
other route under the token has one generic answer (the store proxy is off: no request is relayed
to the manufacturer's servers, and none leaves).

The credential is the device token of the Kobo, in the path: one token per device, of kind
`kobo` (a token of another kind opens nothing here), never a session, never a key, never a header.
The scope is the token's profile, per shelf, re-verified at each page: a child sees nothing above
her ceiling, a book hidden between two pages or between two syncs is exactly an absent one, and
the attacks go through the real application.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from shelfhold.config import Settings
from shelfhold.identity.adapters.kobo_token import kobo_path_token
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.logging import redact_value, redact_url, route_of
from shelfhold.infrastructure.schema import catalog, export, identity, reading
from shelfhold.reading.application.kobo_sync import entitlement_of
from tests.auth_env import AuthEnv, auth_env
from tests.integration.test_l48_opds import auth_of
from tests.visibility_env import HIDDEN_TITLES, Seed, add_profile, seed_catalogue

pytestmark = pytest.mark.req("EF-INT-012", "EF-USR-006", "EF-USR-008", "ENF-SEC-010")

SYNC = "/v1/library/sync"
INIT = "/v1/initialization"
AUTH_DEVICE = "/v1/auth/device"
RATED = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
# the editions of the seed a child with a ceiling of 10 sees
KID_KEYS = ("ok", "ok_second", "adult_shelf", "mixed_rated")


@dataclass
class Kobo:
    """A profile's Kobo: its own address, its own device token of kind `kobo`."""

    name: str
    client: TestClient
    public_id: str
    secret: str

    @property
    def token(self) -> str:
        return kobo_path_token(self.public_id, self.secret)

    @property
    def base(self) -> str:
        return f"/kobo/{self.token}"

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.client.get(f"{self.base}{path}", **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.client.post(f"{self.base}{path}", **kwargs)


@dataclass
class World:
    env: AuthEnv
    seed: Seed
    kid: Kobo
    mum: Kobo

    @property
    def markers(self) -> list[str]:
        """What must never reach the child: titles and identifiers of everything hidden."""
        keys = set(HIDDEN_TITLES) | {"twelve"}
        found = [self.seed.title_of(k) for k in keys] + [self.seed.works[k] for k in keys]
        found += [
            self.seed.editions[k]
            for k in ("above", "adult_edition", "misfiled", "mixed_new", "reserved", "twelve")
        ]
        return found

    def clean(self, text: str) -> None:
        leaked = [m for m in self.markers if m in text]
        assert not leaked, f"a child's answer holds {leaked}"

    def user_id(self, username: str) -> str:
        with self.seed.engine.connect() as connection:
            return str(
                connection.execute(
                    sa.select(identity.user.c.id).where(identity.user.c.username == username)
                ).scalar_one()
            )

    def device_id(self, kobo: Kobo) -> str:
        with self.seed.engine.connect() as connection:
            return str(
                connection.execute(
                    sa.select(identity.device_token.c.device_id).where(
                        identity.device_token.c.public_id == kobo.public_id
                    )
                ).scalar_one()
            )

    def sync_points(self) -> list[Any]:
        with self.seed.engine.connect() as connection:
            return list(connection.execute(sa.select(reading.kobo_sync_point)))

    def write(self, statement: Any) -> None:
        with write_transaction(self.seed.engine) as connection:
            connection.execute(statement)

    def titles_of_library(self) -> list[str]:
        """Every edition's Work title, straight from the catalogue: the ground truth."""
        with self.seed.engine.connect() as connection:
            rows = connection.execute(
                sa.select(catalog.work.c.title)
                .select_from(catalog.edition)
                .join(catalog.work, catalog.work.c.id == catalog.edition.c.work_id)
                .order_by(catalog.work.c.title)
            ).all()
        return [str(row[0]) for row in rows]


def _kobo_of(env: AuthEnv, name: str, ip: str) -> Kobo:
    created = auth_of(env).create_device_token(subject_username=name, name="liseuse", kind="kobo")
    return Kobo(name, env.other_client(ip), created.public_id, created.secret)


@contextmanager
def kobo_world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with auth_env(monkeypatch, settings) as env:
        seed = seed_catalogue(env)
        add_profile(env, "kid", "child", "10.0.0.2", max_age_rating=10)
        add_profile(env, "mum", "member", "10.0.0.3")
        kid = _kobo_of(env, "kid", "10.0.0.20")
        mum = _kobo_of(env, "mum", "10.0.0.30")
        yield World(env, seed, kid, mum)
        seed.engine.dispose()


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with kobo_world(monkeypatch, settings) as made:
        yield made


# -- the calls of a device ----------------------------------------------------------------------


def sync_all(kobo: Kobo, token: str | None = None) -> tuple[list[Any], str]:
    """Every page of one sync, following `x-kobo-synctoken` and `X-Kobo-sync: continue`."""
    pages: list[Any] = []
    headers = {} if token is None else {"x-kobo-synctoken": token}
    while True:
        response = kobo.get(SYNC, headers=headers)
        assert response.status_code == 200, response.text
        pages.append(response)
        token = response.headers["x-kobo-synctoken"]
        headers = {"x-kobo-synctoken": token}
        if response.headers.get("x-kobo-sync") != "continue":
            return pages, token


def result_of(page: Any) -> dict[str, Any]:
    return page.json()["SyncStateResult"]


def books_of(pages: list[Any], *keys: str) -> dict[str, Any]:
    """The entitlements of the pages by list (`NewEntitlements`, `ChangedEntitlements`...)."""
    found: dict[str, Any] = {}
    for page in pages:
        for list_name in keys or ("NewEntitlements",):
            for wrapped in result_of(page)[list_name]:
                entitlement = wrapped["Entitlement"]
                found[entitlement["EntitlementId"]] = entitlement
    return found


def titles_of(books: dict[str, Any]) -> list[str]:
    return sorted(e["BookMetadata"]["Title"] for e in books.values())


def tags_of(pages: list[Any], *lists: str) -> dict[str, dict[str, Any]]:
    """The tags of the pages: tag id -> {"name": ..., "items": [...]}."""
    found: dict[str, dict[str, Any]] = {}
    for page in pages:
        for list_name in lists or ("NewTagging",):
            for entry in result_of(page)[list_name]:
                for changed in entry["ChangedTagging"]:
                    tag = changed["Tag"]
                    found[tag["Id"]] = {
                        "name": tag["Name"],
                        "items": sorted(changed.get("Items") or []),
                    }
    return found


def add_work(world: World, key: str, *, age: int = 6, shelf: str = "adult") -> str:
    """A rated Work with one Edition on a shelf of the seed: a book a sync may carry."""
    work_id, edition_id = new_ulid(), new_ulid()
    title = f"Tale of {key}"
    with world.seed.engine.connect() as connection:
        kind = str(
            connection.execute(sa.text("SELECT key FROM work_kind LIMIT 1")).scalar_one()
        )
    world.write(
        sa.insert(catalog.work).values(
            id=work_id, kind=kind, title=title, sort_title=title.lower(),
            age_rating=age, rated_at=RATED,
        )  # fmt: skip
    )
    world.write(
        sa.insert(catalog.edition).values(
            id=edition_id, work_id=work_id, lang="en", translation_type="official",
            media="images", age_rating=age, rated_at=RATED, shelf_id=world.seed.shelves[shelf],
        )  # fmt: skip
    )
    world.editions[key] = edition_id
    world.works[key] = work_id
    return edition_id


def add_position(world: World, kobo: Kobo, edition_id: str, *, percent: float) -> None:
    """A reading position of the profile on an exported file of the Edition."""
    item_id = new_ulid()
    at = datetime(2026, 11, 23, 6, 0, tzinfo=UTC)
    world.write(
        sa.insert(export.export_item).values(id=item_id, edition_id=edition_id, kind="volume")
    )
    world.write(
        sa.insert(reading.reading_position).values(
            id=new_ulid(), user_id=world.user_id(kobo.name), carrier_type="export_item",
            carrier_id=item_id, content_hash="sha256:" + "ab" * 32, locator={"page": 3},
            percent=percent, client_ts=at, server_ts=at,
        )  # fmt: skip
    )


# -- initialization, dummy auth device, catch-all, refusals ---------------------------------------


def test_kobo_initialization_and_dummy_auth_device(world: World) -> None:
    mum = world.mum
    response = mum.get(INIT)
    assert response.status_code == 200, response.text
    body = response.json()
    hrefs = [resource["Href"] for resource in body["Resources"]]
    # our own paths only: the store proxy is off, no resource names the manufacturer's servers
    assert all(href.startswith(f"{mum.base}/") for href in hrefs), hrefs
    assert "https://" not in response.text
    assert any(href.endswith(SYNC) for href in hrefs)
    assert body["User"]["DisplayName"] == "mum"  # the profile of the token, nobody else

    # the dummy activation: accepted, nothing stored (no device, no audit, no setting)
    devices_before = _device_count(world)
    activated = mum.post(AUTH_DEVICE, json={"DeviceId": "x", "Name": "x"})
    assert activated.status_code == 200
    assert _device_count(world) == devices_before

    # any other route under the token: one generic answer, the same for every method
    for method in ("get", "post", "put", "patch", "delete"):
        stub = getattr(mum.client, method)(f"{mum.base}/v1/anything/else")
        assert stub.status_code == 200, (method, stub.text)
        assert stub.json() == {}

    # the credential is the path segment: nothing else opens the surface
    key = auth_of(world.env).create_api_key(name="k", scope="admin").value
    basic = "Basic " + base64.b64encode(f"{mum.public_id}:{mum.secret}".encode()).decode()
    refusals = [
        world.env.client.get(f"{mum.base}{INIT}"),  # a session (the admin's own)
        world.env.other_client("10.0.0.53").get(
            f"{mum.base}{INIT}", headers={"Authorization": f"Bearer {key}"}
        ),
        world.env.other_client("10.0.0.54").get(
            f"{mum.base}{INIT}", headers={"Authorization": basic}
        ),
        mum.client.get(f"/kobo/{'a' * 46}{INIT}"),  # a well-shaped token nobody holds
        mum.client.get(f"/kobo/{mum.public_id}{INIT}"),  # the identifier alone is nobody
    ]
    for refused in refusals:
        assert refused.status_code == 401, (refused.status_code, refused.text[:200])
        world.clean(refused.text)
    # a revoked token opens nothing, from the next request
    auth_of(world.env).revoke_device_token(mum.public_id)
    assert mum.get(INIT).status_code == 401


def _device_count(world: World) -> int:
    with world.seed.engine.connect() as connection:
        return int(
            connection.execute(sa.select(sa.func.count()).select_from(identity.device)).scalar_one()
        )


# -- the sync: entitlements, tags, continuation token --------------------------------------------


def test_kobo_library_sync_entitlements_tags_continuation_token(world: World) -> None:
    mum = world.mum
    # more books than one page: the sync continues page by page
    for index in range(30):
        add_work(world, f"extra{index}")
    expected = sorted(world.titles_of_library())

    pages, done = sync_all(mum)
    assert len(pages) >= 2  # the continuation token was needed to see every book
    assert pages[0].headers.get("x-kobo-sync") == "continue"
    assert pages[0].headers["x-kobo-synctoken"] == pages[0].json()["ContinuationToken"]
    assert titles_of(books_of(pages)) == expected
    # the tags are the shelves open to the profile and nameable to it, with their visible items
    tags = tags_of(pages)
    assert sorted(tag["name"] for tag in tags.values()) == ["Adults"]
    adults = next(iter(tags.values()))
    assert adults["items"] == sorted(
        entitlement_of(world.seed.editions[k])
        for k in ("ok", "ok_second", "twelve", "adult_shelf", "mixed_rated", "above", "x")
    )
    # the sync point of the device holds what it was given
    assert [row.device_id for row in world.sync_points()] == [world.device_id(mum)]

    # up to date: the completion token answers an empty delta
    pages, done_again = sync_all(mum, done)
    assert len(pages) == 1
    assert result_of(pages[0])["NewEntitlements"] == []
    assert result_of(pages[0])["DeletedEntitlements"] == []
    assert pages[0].headers.get("x-kobo-sync") is None

    # a book arrives: a NewEntitlement; a title changes: ChangedProductMetadata
    arrived = add_work(world, "arrived")
    world.write(
        sa.update(catalog.work)
        .where(catalog.work.c.id == world.seed.works["ok"])
        .values(title="Tale of Orchard, retitled")
    )
    pages, done = sync_all(mum, done_again)
    assert titles_of(books_of(pages, "NewEntitlements")) == ["Tale of arrived"]
    assert [entry["BookMetadata"]["Title"] for entry in result_of(pages[0])["ChangedProductMetadata"]] == [  # fmt: skip
        "Tale of Orchard, retitled"
    ]

    # a book goes away: a DeletedEntitlement, its title nowhere in the answer
    world.write(sa.delete(catalog.edition).where(catalog.edition.c.id == arrived))
    pages, done = sync_all(mum, done)
    deleted = result_of(pages[0])["DeletedEntitlements"]
    assert deleted == [entitlement_of(arrived)]  # the identifier the device holds, never a title
    assert "Tale of arrived" not in pages[0].text

    # a book moves to another shelf: the tag that held it changes
    world.write(
        sa.update(catalog.edition)
        .where(catalog.edition.c.id == world.seed.editions["adult_shelf"])
        .values(shelf_id=world.seed.shelves["family"])
    )
    pages, done = sync_all(mum, done)
    changed_tags = tags_of(pages, "ChangedTagging")
    assert [tag["name"] for tag in changed_tags.values()] == ["Adults"]
    assert entitlement_of(world.seed.editions["adult_shelf"]) not in next(
        iter(changed_tags.values())
    )["items"]

    # a reading position of the profile reaches the device as a changed state
    add_position(world, mum, world.seed.editions["mixed_rated"], percent=0.42)
    pages, done = sync_all(mum, done)
    states = result_of(pages[0])["ChangedReadingState"]
    assert [s["EntitlementId"] for s in states] == [
        entitlement_of(world.seed.editions["mixed_rated"])
    ]
    assert states[0]["ReadingState"]["Percent"] == 0.42


# -- one token per device, in the path -----------------------------------------------------------


def test_kobo_per_device_token_in_path(world: World) -> None:
    mum, second = world.mum, _kobo_of(world.env, "mum", "10.0.0.31")
    assert mum.token != second.token  # one token per device, not per profile
    pages, _ = sync_all(mum)
    assert titles_of(books_of(pages))  # the first device syncs
    pages, _ = sync_all(second)
    assert titles_of(books_of(pages))  # the second too, on its own sync point
    assert sorted(row.device_id for row in world.sync_points()) == sorted(
        [world.device_id(mum), world.device_id(second)]
    )

    # a token of another kind of device opens nothing here (its own doors stay shut to this path)
    koreader = auth_of(world.env).create_device_token(
        subject_username="mum", name="liseuse", kind="koreader"
    )
    path = f"/kobo/{kobo_path_token(koreader.public_id, koreader.secret)}{INIT}"
    assert second.client.get(path).status_code == 401

    # a rotated secret ends the old path: the device types the new address
    rotated = auth_of(world.env).rotate_device(world.device_id(mum))
    assert mum.get(INIT).status_code == 401  # the old path token is refused
    renewed = Kobo("mum", mum.client, rotated.public_id, rotated.secret)
    assert renewed.get(INIT).status_code == 200


# -- the scope of a child, re-verified at each page ------------------------------------------------


def test_kobo_sync_respects_child_visibility(world: World) -> None:
    kid = world.kid
    pages, done = sync_all(kid)
    assert titles_of(books_of(pages)) == sorted(world.seed.title_of(k) for k in KID_KEYS)
    for page in pages:
        world.clean(page.text)
    # the shelf of the adults is not nameable to her, the family shelf is closed to her: no tag
    assert tags_of(pages) == {}
    assert [row.device_id for row in world.sync_points()] == [world.device_id(kid)]

    # a book hidden between two syncs is a deleted entitlement, and its title never comes back
    world.write(
        sa.update(catalog.edition)
        .where(catalog.edition.c.id == world.seed.editions["adult_shelf"])
        .values(shelf_id=world.seed.shelves["family"])
    )
    pages, done = sync_all(kid, done)
    assert result_of(pages[0])["DeletedEntitlements"] == [
        entitlement_of(world.seed.editions["adult_shelf"])
    ]
    for page in pages:
        world.clean(page.text)

    # the scope is re-verified at each page: a ceiling lowered mid-sync empties the next pages
    for index in range(30):
        add_work(world, f"small{index}", age=8)  # under 10, above 7
    first = kid.get(SYNC)
    assert first.status_code == 200
    assert first.headers.get("x-kobo-sync") == "continue"
    world.write(
        sa.update(identity.user)
        .where(identity.user.c.username == "kid")
        .values(max_age_rating=7)
    )
    rest, _ = sync_all(kid, first.headers["x-kobo-synctoken"])
    for page in rest:
        assert not [t for t in titles_of(books_of([page])) if t.startswith("Tale of small")]
        world.clean(page.text)
    # and a fresh sync of the same device knows nothing of them anymore
    pages, _ = sync_all(kid)
    assert not [t for t in titles_of(books_of(pages)) if t.startswith("Tale of small")]
    for page in pages:
        world.clean(page.text)


# -- the token is never logged --------------------------------------------------------------------


def test_kobo_token_never_logged(world: World) -> None:
    token = world.mum.token
    url = f"https://reader.example/kobo/{token}{SYNC}"
    assert token not in redact_url(url)
    assert token not in route_of(url)
    # an access line is free text: the segment after /kobo/ is masked there too
    assert token not in redact_value(f"GET /kobo/{token}{SYNC} 200 OK")
    # and the routes themselves say nothing that holds it
    with capture_logs() as logs:
        response = world.mum.get(SYNC)
        assert response.status_code == 200
    assert token not in str(logs)
