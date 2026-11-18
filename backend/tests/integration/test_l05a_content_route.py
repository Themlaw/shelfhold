"""M5 L05a (EF-LEC-005, ENF-SEC-004, EF-USR-006, ENF-SEC-010): `/content/{ticket}/{path}`.

The real application, real sessions. The archives come from a source the test controls (the
adapter that reads a library is attacked in `test_l05a_content_source.py`). The attacks: a ticket
of another profile, of another carrier, forged, expired, a ceiling lowered under it, a session that
ended, a key, a device token, a path that climbs, an entry outside an excerpt, bytes that a browser
could run.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.schema import catalog
from tests.auth_env import AuthEnv, auth_env
from tests.content_env import (
    BOOK,
    PNG,
    FakeArchives,
    add_carrier,
    session_id_of,
    user_id_of,
)
from tests.visibility_env import Seed, add_profile, seed_catalogue

SHELF = "https://shelf.example"
CONTENT = "https://content.example"
PAGES = {"p1.png": PNG, "p2.png": PNG + b"2", "p3.png": PNG + b"3"}


class World:
    def __init__(
        self, env: AuthEnv, seed: Seed, kid: TestClient, mum: TestClient, src: FakeArchives
    ):
        self.env, self.seed, self.kid, self.mum, self.admin, self.src = (
            env,
            seed,
            kid,
            mum,
            env.client,
            src,
        )
        self.access: Any = env.app.state.content_access  # type: ignore[attr-defined]
        self.open_item = add_carrier(seed.engine, seed.editions["ok"], format="epub")
        self.hidden_item = add_carrier(seed.engine, seed.editions["twelve"], format="epub")
        for item in (self.open_item, self.hidden_item):
            src.by_content[item] = {**BOOK, **PAGES}

    def ticket(self, who: str = "admin", carrier: str | None = None, **kw: Any) -> str:
        user = {"admin": "admin", "kid": "kid", "mum": "mum"}[who]
        issued = self.access.issue(
            user_id=user_id_of(self.seed.engine, user),
            session_id=session_id_of(self.seed.engine, user),
            carrier_type="content",
            carrier_id=carrier or self.open_item,
            **kw,
        )
        return str(issued.token)

    def get(self, client: TestClient, ticket: str, path: str, **kw: Any) -> Any:
        return client.get(f"/content/{ticket}/{path}", **kw)


def _world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    src = FakeArchives()
    monkeypatch.setattr("shelfhold.main.readable_copies", lambda *_a, **_k: src)
    with auth_env(monkeypatch, settings) as env:
        seed = seed_catalogue(env)
        kid = add_profile(env, "kid", "child", "10.0.0.2", max_age_rating=10)
        mum = add_profile(env, "mum", "member", "10.0.0.3")
        assert env.login().status_code == 200
        yield World(env, seed, kid, mum, src)
        seed.engine.dispose()


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    yield from _world(monkeypatch, settings)


def _shape(response: Any) -> tuple[Any, ...]:
    return response.status_code, response.json().get("code"), len(response.content)


@pytest.mark.req("EF-LEC-005", "ENF-SEC-004")
def test_content_headers_on_every_answer(world: World) -> None:
    ticket = world.ticket()
    for path in ("OEBPS/cover.png", "OEBPS/ch1.xhtml", "OEBPS/style.css"):
        got = world.get(world.admin, ticket, path)
        assert got.status_code == 200, path
        assert got.headers["content-security-policy"].startswith("sandbox")
        assert "frame-ancestors 'none'" in got.headers["content-security-policy"]
        assert got.headers["x-content-type-options"] == "nosniff"
        assert got.headers["cache-control"].startswith("private")
        assert got.headers["referrer-policy"] == "no-referrer"
    assert "content-disposition" not in world.get(world.admin, ticket, "OEBPS/cover.png").headers


@pytest.mark.req("EF-LEC-005", "ENF-SEC-004")
@pytest.mark.parametrize("path", ["OEBPS/p1.svg", "OEBPS/page.html", "OEBPS/ch1.xhtml"])
def test_svg_served_as_attachment_with_csp_sandbox(world: World, path: str) -> None:
    """On the real route: SVG and HTML are an attachment, without exception (the reader reads by
    `fetch` and makes a `blob:` of it)."""
    got = world.get(world.admin, world.ticket(), path)
    assert got.status_code == 200
    assert got.headers["content-security-policy"].startswith("sandbox")
    assert got.headers["content-disposition"].startswith("attachment")
    assert got.headers["x-content-type-options"] == "nosniff"


@pytest.mark.req("EF-LEC-005", "ENF-SEC-004")
@pytest.mark.parametrize(
    "path",
    [
        "OEBPS/x.js",
        "OEBPS/m.mjs",
        "OEBPS/m.wasm",
        "OEBPS/data.json",
        "OEBPS/blob.php",
        "OEBPS/page.html",
        "OEBPS/p1.svg",
    ],
)
def test_content_never_serves_executable_type(world: World, path: str) -> None:
    got = world.get(world.admin, world.ticket(), path)
    assert got.status_code == 200
    kind = got.headers["content-type"].split(";")[0].strip().lower()
    forbidden = ("javascript", "ecmascript", "wasm", "json", "php", "x-httpd")
    assert not any(word in kind for word in forbidden), kind
    assert kind not in {"text/html", "application/xhtml+xml", "image/svg+xml"} or (
        got.headers["content-disposition"].startswith("attachment")
    )
    assert got.headers["x-content-type-options"] == "nosniff"
    if path.endswith((".js", ".mjs", ".wasm", ".json", ".php")):
        assert kind == "application/octet-stream"


@pytest.mark.req("EF-LEC-005", "ENF-SEC-004")
def test_no_route_serves_untrusted_bytes_as_script(world: World) -> None:
    """Whatever the name an entry carries, no answer of `/content` has a script type, and the one
    of a missing entry is a problem document that loads nothing."""
    ticket = world.ticket()
    for name in sorted(world.src.by_content[world.open_item]):
        got = world.get(world.admin, ticket, name)
        kind = got.headers["content-type"].split(";")[0].lower()
        assert "script" not in kind and "wasm" not in kind, (name, kind)
    missing = world.get(world.admin, ticket, "OEBPS/none.js")
    assert missing.status_code == 404
    assert "script" not in missing.headers["content-type"]


@pytest.mark.req("EF-LEC-005", "EF-USR-006")
def test_content_ticket_bound_to_profile_and_carrier(world: World) -> None:
    ticket = world.ticket("admin")
    assert world.get(world.admin, ticket, "OEBPS/cover.png").status_code == 200
    stolen = world.get(world.mum, ticket, "OEBPS/cover.png")  # another profile: never reusable
    assert stolen.status_code == 404
    assert world.get(world.kid, ticket, "OEBPS/cover.png").status_code == 404
    other = add_carrier(world.seed.engine, world.seed.editions["mixed_rated"], format="epub")
    world.src.by_content[other] = {"only.png": PNG}
    other_ticket = world.ticket("admin", carrier=other)
    assert world.get(world.admin, other_ticket, "only.png").status_code == 200
    assert world.get(world.admin, other_ticket, "OEBPS/cover.png").status_code == 404  # its own
    flipped = ticket[:-1] + ("A" if ticket[-1] != "A" else "B")  # a forged tag
    assert world.get(world.admin, flipped, "OEBPS/cover.png").status_code == 404
    assert world.get(world.admin, "x" * len(ticket), "OEBPS/cover.png").status_code == 404


@pytest.mark.req("EF-LEC-005")
def test_content_ticket_expired_refused(world: World) -> None:
    ticket = world.ticket()
    assert world.get(world.admin, ticket, "OEBPS/cover.png").status_code == 200
    world.env.clock.advance(timedelta(minutes=11))
    late = world.get(world.admin, ticket, "OEBPS/cover.png")
    assert late.status_code == 404 and "etag" not in late.headers
    assert world.get(world.admin, world.ticket(), "OEBPS/cover.png").status_code == 200  # renewed


@pytest.mark.req("EF-LEC-005", "ENF-SEC-010")
def test_content_ticket_dies_with_its_session(world: World) -> None:
    ticket = world.ticket("mum")
    assert world.get(world.mum, ticket, "OEBPS/cover.png").status_code == 200
    again = world.env.other_client("10.0.0.3")  # a second session of the same profile
    assert world.env.login("mum", client=again).status_code == 200
    assert world.get(again, ticket, "OEBPS/cover.png").status_code == 404  # not the session's own
    assert world.env.post("/api/v1/auth/logout", client=world.mum).status_code in {200, 204}
    assert world.get(world.mum, ticket, "OEBPS/cover.png").status_code in {401, 404}


@pytest.mark.req("EF-LEC-005", "EF-USR-006", "ENF-SEC-010")
def test_content_excerpt_ticket_refuses_out_of_bound_entry(world: World) -> None:
    ticket = world.ticket(scope="excerpt", entries=["p1.png", "p2.png", "OEBPS/ch1.xhtml"])
    assert world.get(world.admin, ticket, "p1.png").status_code == 200
    assert world.get(world.admin, ticket, "OEBPS/ch1.xhtml").status_code == 200
    absent = world.get(world.admin, ticket, "nothing.png")
    for path in ("p3.png", "OEBPS/ch2.xhtml", "OEBPS/style.css"):  # in the archive, not in the list
        refused = world.get(world.admin, ticket, path)
        assert _shape(refused) == _shape(absent), path  # no oracle on what exists
        assert "etag" not in refused.headers
    full = world.ticket(scope="full")
    assert world.get(world.admin, full, "p3.png").status_code == 200  # the bound is the ticket's


@pytest.mark.req("EF-LEC-005", "ENF-SEC-010")
def test_content_excerpt_without_a_list_is_refused(world: World) -> None:
    for entries in (None, []):
        with pytest.raises(ValueError):
            world.ticket(scope="excerpt", entries=entries)


@pytest.mark.req("EF-LEC-005", "EF-USR-006", "ENF-SEC-010")
def test_content_rechecks_visibility_after_reclassification(world: World) -> None:
    """The ticket of a child, a ceiling lowered or a Work reclassified: the next request is the
    answer of an absent thing, with the same ticket and the same session."""
    ticket = world.ticket("kid")
    assert world.get(world.kid, ticket, "OEBPS/cover.png").status_code == 200
    work = world.seed.works["ok"]
    with world.seed.engine.begin() as connection:
        connection.execute(
            sa.update(catalog.work).where(catalog.work.c.id == work).values(age_rating=16)
        )
    after = world.get(world.kid, ticket, "OEBPS/cover.png")
    assert after.status_code == 404 and "etag" not in after.headers
    assert world.get(world.admin, world.ticket("admin"), "OEBPS/cover.png").status_code == 200


@pytest.mark.req("EF-LEC-005", "EF-USR-006")
def test_content_issue_refused_for_a_carrier_the_profile_cannot_see(world: World) -> None:
    from shelfhold.reading.public import UnknownCarrierError

    with pytest.raises(UnknownCarrierError):
        world.ticket("kid", carrier=world.hidden_item)  # above her ceiling
    with pytest.raises(UnknownCarrierError):
        world.ticket("kid", carrier="01HZZZZZZZZZZZZZZZZZZZZZZZ")  # absent: the same answer


@pytest.mark.req("EF-LEC-005", "ENF-SEC-010")
def test_content_etag_decided_after_authorisation(world: World) -> None:
    ticket = world.ticket("admin")
    first = world.get(world.admin, ticket, "OEBPS/cover.png")
    tag = first.headers["etag"]
    assert (
        world.get(
            world.admin, ticket, "OEBPS/cover.png", headers={"If-None-Match": tag}
        ).status_code
        == 304
    )
    # another profile that presents the tag learns nothing: no 304, no tag
    other = world.get(world.mum, ticket, "OEBPS/cover.png", headers={"If-None-Match": tag})
    assert other.status_code == 404 and "etag" not in other.headers


@pytest.mark.req("EF-LEC-005", "ENF-SEC-004")
@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",
        "..%2F..%2Fetc%2Fpasswd",
        "%2e%2e/%2e%2e/etc/passwd",
        "OEBPS/../ch1.xhtml",
        "/etc/passwd",
        "OEBPS//ch1.xhtml",
        "OEBPS%5Cch1.xhtml",
        "OEBPS/ch1.xhtml%00.png",
        "OEBPS/ch1.xhtml/",
        "",
    ],
)
def test_content_path_traversal_refused(world: World, path: str) -> None:
    ticket = world.ticket()
    got = world.get(world.admin, ticket, path)
    assert got.status_code == 404
    assert world.src.asked == []  # the archive was asked for nothing: only exact names are read
    assert "etag" not in got.headers


@pytest.mark.req("EF-LEC-005", "ENF-SEC-010")
def test_content_refuses_api_key_and_device_token(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = world.ticket("admin")
    auth = local_auth(world.seed.engine, world.env.settings)
    for read_only in (True, False):
        key = auth.create_api_key(name=f"k{read_only}", username="admin", read_only=read_only).value
        bare = world.env.other_client("10.0.0.9")
        got = world.get(bare, ticket, "OEBPS/cover.png", headers={"Authorization": f"Bearer {key}"})
        assert got.status_code in {401, 403} and got.headers["content-type"] != "image/png"
        assert got.json()["code"] in {"credential_not_allowed", "forbidden", "unauthenticated"}
    created = auth.create_device_token(subject_username="mum", name="liseuse")  # M4 L46
    raw = base64.b64encode(f"{created.public_id}:{created.secret}".encode()).decode()
    for headers in (
        {"Authorization": f"Basic {raw}"},
        {"Authorization": f"Bearer {created.secret}"},
        {
            "x-auth-user": created.public_id,
            "x-auth-key": hashlib.md5(created.secret.encode()).hexdigest(),  # noqa: S324
        },
    ):
        bare = world.env.other_client("10.0.0.8")
        got = world.get(bare, world.ticket("mum"), "OEBPS/cover.png", headers=headers)
        assert got.status_code in {401, 403, 404} and got.headers["content-type"] != "image/png"
    anonymous = world.env.other_client("10.0.0.7")
    assert world.get(anonymous, ticket, "OEBPS/cover.png").status_code in {
        401,
        404,
    }  # no origin of its own


@pytest.mark.req("EF-LEC-005", "ENF-SEC-004")
def test_book_resources_served_from_dedicated_origin_with_csp_sandbox(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """`SHELFHOLD_CONTENT_ORIGIN` set (D-P2): the ticket alone, bound to its session, is the
    credential (the cookie is not sent there), CORS admits the origin of the SPA and nothing else,
    never with credentials, and the answers keep the sandbox."""
    configured = settings.model_copy(update={"public_url": SHELF, "content_origin": CONTENT})
    for world in _world(monkeypatch, configured):
        ticket = world.ticket("admin")
        url = world.access.url(ticket, "OEBPS/cover.png")
        assert url == f"{CONTENT}/content/{ticket}/OEBPS/cover.png"
        cookieless = TestClient(world.env.app, base_url=CONTENT)  # type: ignore[arg-type]
        got = world.get(cookieless, ticket, "OEBPS/cover.png", headers={"Origin": SHELF})
        assert got.status_code == 200
        assert got.headers["content-security-policy"].startswith("sandbox")
        assert got.headers["access-control-allow-origin"] == SHELF
        assert "access-control-allow-credentials" not in got.headers
        evil = world.get(
            cookieless, ticket, "OEBPS/cover.png", headers={"Origin": "https://evil.example"}
        )
        assert "access-control-allow-origin" not in evil.headers
        preflight = cookieless.options(
            f"/content/{ticket}/OEBPS/cover.png",
            headers={"Origin": SHELF, "Access-Control-Request-Method": "GET"},
        )
        assert preflight.headers.get("access-control-allow-origin") == SHELF
        assert "access-control-allow-credentials" not in preflight.headers
        # the session behind the ticket ended: the cookieless request goes with it
        assert world.env.post("/api/v1/auth/logout").status_code in {200, 204}
        assert world.get(cookieless, ticket, "OEBPS/cover.png").status_code == 404


@pytest.mark.req("EF-LEC-005")
def test_content_without_an_origin_of_its_own_needs_the_session_cookie(world: World) -> None:
    ticket = world.ticket()
    bare = TestClient(world.env.app, base_url="https://testserver")  # type: ignore[arg-type]
    assert world.get(bare, ticket, "OEBPS/cover.png").status_code in {401, 404}
    assert (
        "access-control-allow-origin"
        not in world.get(world.admin, ticket, "OEBPS/cover.png", headers={"Origin": SHELF}).headers
    )


@pytest.mark.req("ENF-SEC-004", "ENF-SEC-010", "EF-USR-024")
def test_reserved_routes_require_fresh_auth_from_reader_session(world: World) -> None:
    """The defence in depth of ADR-0055: a session that is older than `fresh_auth_s` reaches no
    reserved action, whatever it is made to hold. Every route that declares one is attacked."""
    from tests.integration.test_auth_keys_reserved import _reserved_routes

    add_profile(world.env, "kid2", "child", "10.0.0.6", max_age_rating=10)  # a restricted profile
    ticket = world.ticket("admin", scope="full")
    assert world.get(world.admin, ticket, "OEBPS/cover.png").status_code == 200
    world.env.clock.advance(timedelta(seconds=world.env.settings.fresh_auth_s + 60))
    reserved = _reserved_routes(world.env.app)
    assert reserved, "no reserved route found: the scan is blind"
    headers = {"X-CSRF-Token": world.env.csrf()}
    from shelfhold.identity.domain.catalogue import RESERVED_ACTIONS

    stale = 0
    for method, route_path, action in reserved:
        path = route_path.replace("{", "x").replace("}", "")
        got = world.admin.request(method, path, json={}, headers=headers)
        needs = RESERVED_ACTIONS[action].needs_fresh_auth(
            restricted_profile_exists=True, hardened=False
        )  # a read of a list asks for no fresh login: the rule is the catalogue's
        if needs:
            stale += 1
            assert got.status_code in {401, 403}, (method, route_path, action, got.status_code)
            if got.status_code == 401:  # (403: refused for another reason, e.g. made on the host)
                assert got.json()["code"] == "reauth_required", (method, route_path, action)
    assert stale, "no reserved route that needs a fresh login: the scan is blind"


@pytest.mark.req("EF-LEC-005", "ENF-SEC-004")
def test_content_ticket_absent_from_logs(world: World, capfd: pytest.CaptureFixture[str]) -> None:
    ticket = world.ticket()
    for path in ("OEBPS/cover.png", "OEBPS/none.png"):
        world.get(world.admin, ticket, path)
    seen = capfd.readouterr()
    assert ticket not in seen.out + seen.err and ticket.split(".")[0] not in seen.out + seen.err
