"""M3 L24 (EF-BIB-020, EF-USR-006, ENF-SEC-010): `GET /api/v1/thumbnails/{hash}/{size}`.

The attacks: a child that presents the ETag of an adult cover, a hash she guesses, a key (hers or
an adult's), a ceiling lowered under a cached answer, a verb that is not a read, and an
unreferenced cover. Authorisation is decided before anything else is computed: before the ETag is
compared and before a single pixel is made.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.schema import storage
from tests.auth_env import CSRF_HEADER, AuthEnv, auth_env
from tests.thumbnail_env import put_cover, thumbnail_files
from tests.visibility_env import Seed, add_profile, seed_catalogue

# a child's ceiling is 10: see `tests/visibility_env.py` for what she sees
SEEN = {"ok": (220, 20, 20), "mixed_rated": (20, 220, 20)}
HIDDEN = {
    "twelve": (20, 20, 220),  # above her ceiling
    "above": (220, 220, 20),  # far above
    "adult_edition": (220, 20, 220),  # an adult Edition of a rated Work
    "misfiled": (20, 220, 220),  # an unrated Work
    "mixed_new": (120, 120, 120),  # an unrated Edition of a rated Work
}
ABSENT = "ab" * 32
CACHE = "private, max-age=86400, immutable"


def url(digest: str, size: int = 320) -> str:
    return f"/api/v1/thumbnails/{digest}/{size}"


class World:
    def __init__(self, env: AuthEnv, seed: Seed, kid: TestClient, mum: TestClient) -> None:
        self.env, self.seed, self.kid, self.mum, self.admin = env, seed, kid, mum, env.client
        self.digests: dict[str, str] = {}
        with seed.engine.begin() as connection:
            for key, colour in {**SEEN, **HIDDEN}.items():
                self.digests[key] = put_cover(
                    env.settings.data_dir, connection, seed.editions[key], colour
                )

    @property
    def files(self) -> list[Any]:
        return thumbnail_files(self.env.settings.data_dir)

    def key(self, username: str, name: str) -> str:
        return (
            local_auth(self.seed.engine, self.env.settings)
            .create_api_key(name=name, username=username, read_only=True)
            .value
        )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with auth_env(monkeypatch, settings) as env:
        seed = seed_catalogue(env)
        kid = add_profile(env, "kid", "child", "10.0.0.2", max_age_rating=10)
        mum = add_profile(env, "mum", "member", "10.0.0.3")
        assert env.login().status_code == 200
        yield World(env, seed, kid, mum)
        seed.engine.dispose()


def _shape(response: Any) -> tuple[Any, ...]:
    body = response.json()
    return response.status_code, body.get("type"), body.get("title"), body.get("detail")


@pytest.mark.req("EF-BIB-020")
def test_thumbnail_cache_control_immutable_etag(world: World) -> None:
    first = world.admin.get(url(world.digests["ok"]))
    assert first.status_code == 200
    assert first.headers["content-type"] == "image/webp"
    assert first.content[:4] == b"RIFF" and first.content[8:12] == b"WEBP"
    assert first.headers["cache-control"] == CACHE
    assert "cookie" in first.headers["vary"].lower()
    assert first.headers["x-content-type-options"] == "nosniff"
    etag = first.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"') and etag != '""'
    again = world.admin.get(url(world.digests["ok"]), headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""
    assert again.headers["etag"] == etag and again.headers["cache-control"] == CACHE
    assert "cookie" in again.headers["vary"].lower()
    listed = world.admin.get(url(world.digests["ok"]), headers={"If-None-Match": f'"x", {etag}'})
    assert listed.status_code == 304  # a list of tags, the weak form of ours
    weak = world.admin.get(url(world.digests["ok"]), headers={"If-None-Match": f"W/{etag}"})
    assert weak.status_code == 304
    star = world.admin.get(url(world.digests["ok"]), headers={"If-None-Match": "*"})
    assert star.status_code == 304
    other = world.admin.get(url(world.digests["ok"]), headers={"If-None-Match": '"not-it"'})
    assert other.status_code == 200 and other.content == first.content
    sizes = {
        s: world.admin.get(url(world.digests["ok"], s)).headers["etag"] for s in (160, 320, 640)
    }
    assert len(set(sizes.values())) == 3  # each size has its own tag
    assert world.admin.get(url(world.digests["ok"], 160)).headers["etag"] != etag


@pytest.mark.req("EF-BIB-020", "EF-USR-006", "ENF-SEC-010")
def test_thumbnail_private_authz_before_etag(world: World) -> None:
    """The acceptance: a child that presents the ETag of an adult cover gets 404, never 304; and
    that 404 is the one of a hash that does not exist."""
    absent = world.kid.get(url(ABSENT))
    assert absent.status_code == 404
    for key, digest in world.digests.items():
        etag = world.admin.get(url(digest)).headers["etag"]
        before = len(world.files)
        for conditional in ({}, {"If-None-Match": etag}, {"If-None-Match": "*"}):
            for size in (160, 320, 640):
                got = world.kid.get(url(digest, size), headers=conditional)
                if key in SEEN:
                    continue
                assert got.status_code == 404, (key, conditional)
                assert _shape(got) == _shape(absent), (key, conditional)
                assert "etag" not in got.headers and "immutable" not in got.headers.get(
                    "cache-control", ""
                )
                assert digest not in got.text and world.seed.editions[key] not in got.text
        if key not in SEEN:
            assert len(world.files) == before  # nothing was made for a reader who may not see it
    seen = world.digests["ok"]
    etag = world.admin.get(url(seen)).headers["etag"]
    assert world.kid.get(url(seen)).status_code == 200  # the control: what is hers is served
    assert world.kid.get(url(seen), headers={"If-None-Match": etag}).status_code == 304


@pytest.mark.req("EF-BIB-020", "EF-USR-006")
def test_thumbnail_hidden_for_the_child_but_served_to_an_adult(world: World) -> None:
    for key, digest in world.digests.items():
        assert world.admin.get(url(digest)).status_code == 200, key
        assert world.mum.get(url(digest)).status_code == 200, key  # a member is an adult reader


@pytest.mark.req("EF-BIB-020", "EF-USR-006")
def test_thumbnail_cover_shared_with_a_visible_edition_is_visible(world: World) -> None:
    """The same image under a hidden Edition and a visible one: the visible one entitles her."""
    colour = HIDDEN["twelve"]
    with world.seed.engine.begin() as connection:
        shared = put_cover(
            world.env.settings.data_dir, connection, world.seed.editions["ok_second"], colour
        )
    assert shared == world.digests["twelve"]
    assert world.kid.get(url(shared)).status_code == 200  # `ok_second` is an Edition of "ok"


@pytest.mark.req("EF-BIB-020", "EF-USR-006")
def test_thumbnail_a_lowered_ceiling_applies_on_the_next_request(world: World) -> None:
    digest = world.digests["ok"]
    etag = world.kid.get(url(digest)).headers["etag"]
    assert world.kid.get(url(digest), headers={"If-None-Match": etag}).status_code == 304
    world.env.app.state.auth_gate.auth.profile_set(  # type: ignore[attr-defined]
        str(_kid_id(world)), max_age_rating=0
    )
    after = world.kid.get(url(digest), headers={"If-None-Match": etag})
    assert after.status_code == 404  # the tag she holds opens nothing any more


def _kid_id(world: World) -> str:
    with world.seed.engine.connect() as connection:
        return str(connection.execute(sa.text("SELECT id FROM user WHERE username='kid'")).scalar())


@pytest.mark.req("EF-BIB-020", "ENF-SEC-010")
def test_thumbnail_refused_to_keys_and_to_nobody(world: World) -> None:
    """A browser shows a cover with its cookie. A key (hers, an adult's) and a request with no
    credential get nothing, and nothing is made for them."""
    digest = world.digests["ok"]
    kid_key, admin_key = world.key("kid", "kidkey"), world.key("admin", "adminkey")
    for key, ip in ((kid_key, "10.0.0.50"), (admin_key, "10.0.0.51")):
        for target in (digest, world.digests["twelve"], ABSENT):
            got = world.env.other_client(ip).get(
                url(target), headers={"Authorization": f"Bearer {key}"}
            )
            assert got.status_code == 403, target
            assert got.json()["type"].endswith("credential-not-allowed")
            assert "etag" not in got.headers
    anonymous = world.env.other_client("10.0.0.60").get(url(digest))
    assert anonymous.status_code == 401 and "etag" not in anonymous.headers
    assert world.files == []


@pytest.mark.req("EF-BIB-020", "ENF-SEC-010")
def test_thumbnail_route_reads_only(world: World) -> None:
    digest = world.digests["ok"]
    for client in (world.admin, world.mum, world.kid):
        for method in ("post", "put", "patch", "delete"):
            got = client.request(method, url(digest), headers={CSRF_HEADER: world.env.csrf(client)})
            assert got.status_code in (403, 405), (method, got.status_code)
    assert world.files == [] and _rows(world) == 0


def _rows(world: World) -> int:
    with world.seed.engine.connect() as connection:
        return int(
            connection.execute(
                sa.select(sa.func.count()).select_from(storage.thumbnail)
            ).scalar_one()
        )


@pytest.mark.req("EF-BIB-020", "ENF-SEC-010")
def test_thumbnail_unreferenced_cover_malformed_hash_and_size_are_not_found(world: World) -> None:
    data = world.env.settings.data_dir
    orphan_bytes = b"orphan cover bytes"
    orphan = hashlib.sha256(orphan_bytes).hexdigest()
    (data / "covers").mkdir(parents=True, exist_ok=True)
    (data / "covers" / f"{orphan}.webp").write_bytes(orphan_bytes)
    digest = world.digests["ok"]
    for client in (world.admin, world.mum, world.kid):
        for path in (
            url(orphan),
            url(ABSENT),
            url(digest.upper()),
            url(digest[:-1]),
            url("..%2f" * 3),
            url(digest, 100),
            url(digest, 0),
            url(digest, 1280),
            f"/api/v1/thumbnails/{digest}/x",
            f"/api/v1/thumbnails/{digest}.webp/320",
            f"/api/v1/thumbnails/sha256:{digest}/320",
        ):
            got = client.get(path)
            assert got.status_code in (404, 422), (path, got.status_code)
            assert "etag" not in got.headers
    assert world.files == []


@pytest.mark.req("EF-BIB-020")
def test_thumbnail_of_a_cover_file_gone_or_broken_is_not_found_and_not_cached(
    world: World,
) -> None:
    data = world.env.settings.data_dir
    (data / "covers" / f"{world.digests['ok']}.webp").unlink()
    got = world.admin.get(url(world.digests["ok"]))
    assert got.status_code == 404 and "immutable" not in got.headers.get("cache-control", "")
    (data / "covers" / f"{world.digests['above']}.webp").write_bytes(b"swapped bytes")
    assert world.admin.get(url(world.digests["above"])).status_code == 404
    assert world.files == [] and _rows(world) == 0


@pytest.mark.req("EF-BIB-020", "ENF-SEC-010")
def test_thumbnail_route_is_in_the_openapi_document(world: World) -> None:
    document = world.admin.get("/api/v1/openapi.json").json()
    path = document["paths"]["/api/v1/thumbnails/{content_hash}/{size}"]
    assert set(path) == {"get"}
    assert {"200", "304", "404"} <= set(path["get"]["responses"])
