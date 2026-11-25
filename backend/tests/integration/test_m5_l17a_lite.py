"""M5 L17a: the `/lite` pages, server-rendered HTML with no JavaScript, for readers whose browser
cannot run a modern script (EF-LEC-010, ENF-COMP-002 `/lite`; ENF-SEC-010, EF-USR-006).

A reader signs in with the secret of a device token (a form), gets a cookie that holds nothing
but a signed proof for that token, limited to `/lite`, and may read: shelves, series, Works,
download. The profile is read from the database at each request. The attacks go through the real
application: a child's token, a member's, a revoked, a rotated and an expired one, no cookie, a
forged and a tampered one, the web session and an API key of the same profile, a form posted from
another site, another profile's shelf, a hidden Work, Edition and cover, and plain HTTP.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import catalog
from tests.integration.test_l48_opds import (
    ABSENT_ULID,
    HIDDEN_KID,
    PAYLOAD,
    SEEN_KID,
    FakeBundles,
    Reader,
    World,
    auth_of,
    make_world,
)
from tests.visibility_env import HIDDEN_TITLES, VISIBLE_TITLES

pytestmark = pytest.mark.req("EF-LEC-010", "ENF-COMP-002", "ENF-SEC-010", "EF-USR-006")

CSRF_FIELD = re.compile(r'name="csrf"\s+value="([^"]+)"')
HTTP = "http://testserver"
LITE_TEMPLATES = Path(__file__).parents[2] / "src/shelfhold/interfaces/http_api/lite/templates"


class Links(HTMLParser):
    """What a page points at: `a href`, `img src`, `form action`, and every tag it opens."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.images: list[str] = []
        self.forms: list[tuple[str, str]] = []
        self.tags: list[str] = []
        self.attributes: list[str] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        values = {name: value or "" for name, value in attrs}
        self.attributes += [name for name, _ in attrs]
        if tag == "a" and "href" in values:
            self.hrefs.append(values["href"])
        if tag == "img" and "src" in values:
            self.images.append(values["src"])
        if tag == "form":
            self.forms.append((values.get("method", "get").lower(), values.get("action", "")))


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with make_world(monkeypatch, settings, FakeBundles()) as made:
        yield made


def browser(world: World, ip: str, base_url: str = "https://testserver") -> TestClient:
    return world.env.other_client(ip, base_url)


def sign_in(client: TestClient, reader: Reader, **override: str) -> Any:
    page = client.get("/lite/sign-in")
    found = CSRF_FIELD.search(page.text)
    assert found, page.text
    data = {
        "identifier": reader.public_id,
        "secret": reader.secret,
        "csrf": found.group(1),
        **override,
    }
    return client.post("/lite/sign-in", data=data, follow_redirects=False)


def signed_in(world: World, reader: Reader, ip: str) -> TestClient:
    client = browser(world, ip)
    done = sign_in(client, reader)
    assert done.status_code == 303 and done.headers["location"].rstrip("/").endswith("/lite")
    return client


def crawl(client: TestClient, start: str = "/lite") -> dict[str, Any]:
    """Every page reached by links alone (no form, no script): {path: response}."""
    seen: dict[str, Any] = {}
    todo = [start]
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        response = client.get(path)
        seen[path] = response
        if response.status_code != 200 or "text/html" not in response.headers["content-type"]:
            continue
        page = Links(response.text)
        for href in page.hrefs:
            if href.startswith("/lite") and "/complete" not in href and href not in seen:
                todo.append(href)
    return seen


def pages(found: dict[str, Any]) -> str:
    return "\n".join(r.text for r in found.values() if "text/html" in r.headers["content-type"])


# -- EF-LEC-010: the acceptance ----------------------------------------------------------------


def test_lite_page_works_without_javascript(world: World) -> None:
    """A browser that runs no script signs in with a form, browses by links and downloads."""
    client = browser(world, "10.0.1.1")
    form = Links(client.get("/lite/sign-in").text)
    assert ("post", "/lite/sign-in") in form.forms  # a plain form: no script submits it
    done = sign_in(client, world.mum)
    assert done.status_code == 303
    home = client.get("/lite")
    assert home.status_code == 200 and "text/html" in home.headers["content-type"]
    found = crawl(client)
    paths = set(found)
    assert {"/lite/shelves", "/lite/series", "/lite/works", "/lite/recent"} <= {
        p.split("?")[0] for p in paths
    }
    assert all(r.status_code == 200 for r in found.values()), {
        p: r.status_code for p, r in found.items()
    }
    text = pages(found)
    for title in (*VISIBLE_TITLES.values(), *HIDDEN_TITLES.values()):
        assert title in text  # a member is not narrowed
    work = f"/lite/works/{world.seed.works['ok']}"
    assert work in paths
    download = next(h for h in Links(found[work].text).hrefs if "/complete" in h)
    got = client.get(download)
    assert got.status_code == 200 and got.content == PAYLOAD
    assert "attachment" in got.headers["content-disposition"]
    assert got.headers["content-security-policy"].startswith("sandbox")
    assert got.headers["cache-control"] == "no-store"


def test_lite_pages_contain_no_script(world: World) -> None:
    """The CI control (GAP-4-18): no page of `/lite` holds a script, an inline handler, an
    embedded frame or a `javascript:` link; no template does either; the CSP allows none."""
    for ip, reader in (("10.0.1.2", world.kid), ("10.0.1.3", world.mum)):
        client = browser(world, ip)
        shown = [client.get("/lite/sign-in"), client.get("/lite"), client.get("/lite/nothing-here")]
        sign_in(client, reader)
        found = crawl(client)
        found["/lite/zz404"] = client.get(f"/lite/works/{ABSENT_ULID}")
        shown += list(found.values())
        for response in shown:
            assert "text/html" in response.headers["content-type"], response.request.url
            page = Links(response.text)
            assert not {"script", "iframe", "object", "embed", "applet", "frame"} & set(page.tags)
            assert not [a for a in page.attributes if a.startswith("on")]
            assert "javascript:" not in response.text.lower()
            assert "<script" not in response.text.lower()
            csp = response.headers["content-security-policy"]
            assert "default-src 'none'" in csp and "'unsafe-inline'" not in csp
            assert "script-src" not in csp
            assert "'unsafe-eval'" not in csp
    templates = sorted(LITE_TEMPLATES.glob("*.html"))
    assert 4 <= len(templates) <= 8  # five, and one of small pieces
    for template in templates:
        source = template.read_text("utf-8").lower()
        assert "<script" not in source and "javascript:" not in source, template.name
        assert not re.search(r"\son[a-z]+\s*=", source), template.name


# -- the cookie ---------------------------------------------------------------------------------


def scrub(response: Any) -> str:
    """The page without the token of its form (a new one is drawn for each browser)."""
    return CSRF_FIELD.sub("", response.text)


def _cookies(response: Any) -> list[str]:
    return response.headers.get_list("set-cookie")


def test_lite_cookie_scoped_to_lite_and_read_only(world: World) -> None:
    client = browser(world, "10.0.1.4")
    done = sign_in(client, world.mum)
    lines = [c for c in _cookies(done) if "lite" in c.split("=", 1)[0].lower()]
    assert lines, _cookies(done)
    session = next(c for c in lines if "flow" not in c.split("=", 1)[0].lower())
    attributes = {a.strip().lower() for a in session.split(";")[1:]}
    assert "path=/lite" in attributes and "httponly" in attributes and "secure" in attributes
    assert "samesite=strict" in attributes
    assert not any(a.startswith("domain=") for a in attributes)
    for line in lines:
        assert "path=/lite" in line.lower()
    name, value = session.split(";", 1)[0].split("=", 1)
    assert world.mum.secret not in session  # a proof for the token, never the secret
    # sent by hand to everything that is not `/lite`: nobody (a cookie of another door)
    other = browser(world, "10.0.1.5")
    for path in ("/api/v1/me", "/api/v1/works", "/opds", "/opds/works", "/kosync/users/auth"):
        refused = other.get(path, headers={"Cookie": f"{name}={value}"})
        assert refused.status_code in (401, 403), (path, refused.status_code)
    # nothing is written through it: no verb but GET anywhere, but the two forms of `/lite`
    for method in ("PUT", "PATCH", "DELETE"):
        assert client.request(method, "/lite/works").status_code in (404, 405)
    for path in ("/lite/works", f"/lite/works/{world.seed.works['ok']}", "/lite/shelves"):
        assert client.post(path).status_code in (403, 404, 405)
    app_routes = set()
    for context in iter_route_contexts(world.env.app.routes):  # routers included lazily too
        own = context.methods or getattr(context.original_route, "methods", None) or set()
        if context.path and context.path.startswith("/lite"):
            app_routes |= {(method, context.path) for method in own - {"HEAD", "OPTIONS"}}
    assert ("GET", "/lite") in app_routes
    writes = {path for method, path in app_routes if method != "GET"}
    assert writes == {"/lite/sign-in", "/lite/sign-out"}


def test_lite_forged_and_stale_cookies_open_nothing(world: World) -> None:
    client = signed_in(world, world.mum, "10.0.1.6")
    jar = {c.name: c.value for c in client.cookies.jar if "lite" in c.name.lower()}
    name = next(n for n in jar if "flow" not in n.lower())
    value = jar[name]
    assert client.get("/lite/works").status_code == 200
    anonymous = browser(world, "10.0.1.7")
    payload, _, mac = value.partition(".")
    for forged in (
        "",
        "x",
        value + "0",
        payload + "." + mac[::-1],
        payload[::-1] + "." + mac,
        "." + mac,
        payload + ".",
        "A" * 600,
    ):
        got = anonymous.get(
            "/lite/works", headers={"Cookie": f"{name}={forged}"}, follow_redirects=False
        )
        assert got.status_code in (303, 401, 403), forged
        assert HIDDEN_TITLES["above"] not in got.text and VISIBLE_TITLES["ok"] not in got.text


def test_lite_needs_a_live_token_at_every_request(world: World) -> None:
    """Revoked, rotated, expired: the cookie that was good a moment ago opens nothing."""
    auth = auth_of(world.env)
    for index, how in enumerate(("revoke", "rotate", "expire")):
        created = auth.create_device_token(
            subject_username="mum", name=how, expires_days=1 if how == "expire" else None
        )
        reader = Reader("mum", world.mum.client, created.public_id, created.secret)
        client = signed_in(world, reader, f"10.0.2.{index}")
        assert client.get("/lite/works").status_code == 200
        if how == "revoke":
            assert auth.revoke_device_token(created.public_id)
        elif how == "rotate":
            assert auth.rotate_device(created.device_id) is not None
        else:
            world.env.clock.advance(timedelta(days=2))
        after = client.get("/lite/works", follow_redirects=False)
        assert after.status_code == 303 and "sign-in" in after.headers["location"], how
        assert VISIBLE_TITLES["ok"] not in after.text
        assert sign_in(browser(world, f"10.0.3.{index}"), reader).status_code == 401, how


def test_lite_needs_no_other_credential_and_accepts_none_but_its_own(world: World) -> None:
    """A web session or an API key of the same profile is no way into `/lite`, and a lite cookie
    is no way out of it."""
    key = auth_of(world.env).create_api_key(name="kidkey", username="kid", read_only=True).value
    for client, headers in (
        (world.kid_session, {}),  # her browser session (cookie jar of `sid`, sent to every path)
        (browser(world, "10.0.1.9"), {"Authorization": world.kid.headers["Authorization"]}),
    ):
        got = client.get("/lite/works", headers=headers, follow_redirects=False)
        assert got.status_code == 303 and "sign-in" in got.headers["location"]
        assert VISIBLE_TITLES["ok"] not in got.text
    # an API key is refused, not ignored: a key of a narrow scope reaches nothing else
    refused = browser(world, "10.0.1.8").get(
        "/lite/works", headers={"Authorization": f"Bearer {key}"}, follow_redirects=False
    )
    assert refused.status_code == 403 and VISIBLE_TITLES["ok"] not in refused.text
    # the web session of an adult still works as before; `/lite` did not touch the session
    assert world.kid_session.get("/api/v1/me").status_code == 200


def test_lite_without_a_cookie_serves_nothing_but_the_form(world: World) -> None:
    anonymous = browser(world, "10.0.1.10")
    ids = {
        "work": world.seed.works["ok"],
        "series": world.series["visible"],
        "shelf": world.seed.shelves["family"],
        "edition": world.seed.editions["ok"],
        "digest": world.covers["ok"],
    }
    paths = [
        "/lite",
        "/lite/shelves",
        f"/lite/shelves/{ids['shelf']}",
        "/lite/series",
        f"/lite/series/{ids['series']}",
        "/lite/works",
        "/lite/recent",
        f"/lite/works/{ids['work']}",
        "/lite/search?q=Orchard",
        f"/lite/covers/{ids['digest']}/160",
        f"/lite/editions/{ids['edition']}/complete",
    ]
    for path in paths:
        got = anonymous.get(path, follow_redirects=False)
        assert got.status_code == 303 and "sign-in" in got.headers["location"], path
        assert got.headers["cache-control"] == "no-store"
        assert VISIBLE_TITLES["ok"] not in got.text and "Orchard" not in got.text
    assert world.bundles is not None and world.bundles.opened == []


# -- signing in ---------------------------------------------------------------------------------


def test_lite_sign_in_is_one_answer_for_every_failure_and_never_echoes_the_secret(
    world: World,
) -> None:
    client = browser(world, "10.0.1.11")
    wrong = sign_in(client, world.mum, secret="z" * 26)
    unknown = sign_in(client, world.mum, identifier="zzzzzzzz")
    empty = sign_in(client, world.mum, secret="")
    assert (wrong.status_code, unknown.status_code) == (401, 401)
    assert scrub(wrong) == scrub(unknown)  # nothing tells an unknown identifier from a wrong secret
    assert empty.status_code in (400, 401, 422)
    assert not any(
        "lite" in c.split("=", 1)[0].lower() and "flow" not in c.lower() for c in _cookies(wrong)
    ), _cookies(wrong)
    for response in (wrong, unknown, empty):
        assert "z" * 26 not in response.text and world.mum.secret not in response.text
        assert response.headers["cache-control"] == "no-store"
    # a revoked token is the same answer
    created = auth_of(world.env).create_device_token(subject_username="mum", name="gone")
    assert auth_of(world.env).revoke_device_token(created.public_id)
    gone = sign_in(client, world.mum, identifier=created.public_id, secret=created.secret)
    assert gone.status_code == 401 and scrub(gone) == scrub(wrong)


def test_lite_sign_in_takes_the_secret_as_typed_on_a_small_keyboard(world: World) -> None:
    typed = "-".join(world.mum.secret[i : i + 5] for i in range(0, 26, 5)).upper()
    assert sign_in(browser(world, "10.0.1.12"), world.mum, secret=typed).status_code == 303


def test_lite_sign_in_is_slowed_after_many_wrong_secrets(world: World) -> None:
    client = browser(world, "10.0.1.13")
    codes = [sign_in(client, world.mum, secret=f"{n:026d}").status_code for n in range(40)]
    assert 429 in codes and set(codes) <= {401, 429}
    limited = sign_in(client, world.mum)  # the right secret waits as well while the pair is over
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1


def test_lite_forms_carry_a_csrf_token_and_refuse_a_foreign_one(world: World) -> None:
    client = browser(world, "10.0.1.14")
    good = {"identifier": world.mum.public_id, "secret": world.mum.secret}
    form = client.get("/lite/sign-in")
    token = CSRF_FIELD.search(form.text)
    assert token
    for data in ({}, {"csrf": ""}, {"csrf": "x" * 40}, {"csrf": token.group(1)[::-1]}):
        refused = client.post("/lite/sign-in", data={**good, **data}, follow_redirects=False)
        assert refused.status_code == 403, data
        assert not [c for c in _cookies(refused) if "flow" not in c.lower() and "lite" in c.lower()]
    # a token drawn for another browser is no token for this one
    other = browser(world, "10.0.1.15")
    foreign = CSRF_FIELD.search(other.get("/lite/sign-in").text)
    assert foreign
    refused = client.post(
        "/lite/sign-in", data={**good, "csrf": foreign.group(1)}, follow_redirects=False
    )
    assert refused.status_code == 403
    # a form sent from another site is refused even with a token
    cross = client.post(
        "/lite/sign-in",
        data={**good, "csrf": token.group(1)},
        headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://elsewhere.example"},
        follow_redirects=False,
    )
    assert cross.status_code == 403
    # a cookie-less post (no flow at all) is refused
    bare = browser(world, "10.0.1.16").post(
        "/lite/sign-in", data={**good, "csrf": token.group(1)}, follow_redirects=False
    )
    assert bare.status_code == 403
    assert (
        client.post(
            "/lite/sign-in", data={**good, "csrf": token.group(1)}, follow_redirects=False
        ).status_code
        == 303
    )


def test_lite_sign_in_accepts_the_null_origin_of_a_no_referrer_page(world: World) -> None:
    """Chrome posts a form of a `no-referrer` page with `Origin: null`: that is no foreign origin
    (found by the M5 journey of `/lite`, where no Chrome could sign in). A real foreign origin, or
    a cross-site request, stays refused; so does a `null` that the browser does not vouch for as
    same-origin (a sandboxed page)."""
    client = browser(world, "10.0.1.30")
    token = CSRF_FIELD.search(client.get("/lite/sign-in").text)
    assert token
    good = {"identifier": world.mum.public_id, "secret": world.mum.secret, "csrf": token.group(1)}
    posted = client.post(
        "/lite/sign-in", data=good, headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )  # fmt: skip
    assert posted.status_code == 303
    again = browser(world, "10.0.1.31")
    token = CSRF_FIELD.search(again.get("/lite/sign-in").text)
    assert token
    cross = again.post(
        "/lite/sign-in", data={**good, "csrf": token.group(1)},
        headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"}, follow_redirects=False,
    )  # fmt: skip
    assert cross.status_code == 403


def test_lite_sign_out_is_a_post_with_its_own_token(world: World) -> None:
    client = signed_in(world, world.mum, "10.0.1.17")
    home = client.get("/lite")
    assert ("post", "/lite/sign-out") in Links(home.text).forms
    token = CSRF_FIELD.search(home.text)
    assert token
    assert client.get("/lite/sign-out").status_code in (404, 405)
    assert client.post("/lite/sign-out", follow_redirects=False).status_code == 403
    assert (
        client.post("/lite/sign-out", data={"csrf": "x" * 40}, follow_redirects=False).status_code
        == 403
    )
    assert client.get("/lite/works").status_code == 200  # still in
    cross = client.post(
        "/lite/sign-out",
        data={"csrf": token.group(1)},
        headers={"Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )
    assert cross.status_code == 403
    out = client.post("/lite/sign-out", data={"csrf": token.group(1)}, follow_redirects=False)
    assert out.status_code == 303
    assert client.get("/lite/works", follow_redirects=False).status_code == 303
    # the token of another browser's session is no token here
    first = signed_in(world, world.mum, "10.0.1.18")
    second = signed_in(world, world.mum, "10.0.1.19")
    mine = CSRF_FIELD.search(first.get("/lite").text)
    assert mine
    assert (
        second.post(
            "/lite/sign-out", data={"csrf": mine.group(1)}, follow_redirects=False
        ).status_code
        == 403
    )


def test_lite_refuses_plain_http_before_any_secret_is_read(world: World) -> None:
    client = browser(world, "10.0.1.20", HTTP)
    page = client.get("/lite/sign-in")
    assert page.status_code in (400, 403) and 'name="secret"' not in page.text
    posted = client.post(
        "/lite/sign-in",
        data={"identifier": world.mum.public_id, "secret": world.mum.secret, "csrf": "x"},
        follow_redirects=False,
    )
    assert posted.status_code in (400, 403) and not _cookies(posted)
    assert world.mum.secret not in posted.text


# -- the profile is read at each request --------------------------------------------------------


def test_lite_child_sees_only_visible_works(world: World) -> None:
    client = signed_in(world, world.kid, "10.0.1.21")
    found = crawl(client)
    text = pages(found)
    assert all(r.status_code == 200 for r in found.values())
    for key in SEEN_KID:
        assert world.seed.title_of(key) in text, key
    for key in HIDDEN_KID:
        assert world.seed.title_of(key) not in text, key
    world.clean(text)
    listed = client.get("/lite/works").text
    assert sorted(t for t in (world.seed.title_of(k) for k in SEEN_KID) if t in listed) == sorted(
        world.seed.title_of(k) for k in SEEN_KID
    )
    adult = crawl(signed_in(world, world.mum, "10.0.1.22"))
    assert world.seed.title_of("above") in pages(adult)  # the control: a member sees it


def test_lite_hidden_and_absent_are_one_answer(world: World) -> None:
    client = signed_in(world, world.kid, "10.0.1.23")
    absent = client.get(f"/lite/works/{ABSENT_ULID}")
    assert absent.status_code == 404
    for key in HIDDEN_KID:
        hidden = client.get(f"/lite/works/{world.seed.works[key]}")
        assert (hidden.status_code, hidden.text) == (absent.status_code, absent.text), key
    for path in (
        f"/lite/series/{world.series['mixed']}",  # a series with one hidden member
        f"/lite/shelves/{world.seed.shelves['family']}",  # a shelf reserved to another profile
        f"/lite/covers/{world.covers['above']}/160",
        f"/lite/editions/{world.seed.editions['above']}/complete",
        f"/lite/editions/{world.seed.editions['adult_edition']}/complete",
        f"/lite/editions/{world.seed.editions['misfiled']}/complete",
    ):
        got = client.get(path)
        assert got.status_code == 404, path
        world.clean(got.text)
    for path, empty in (
        (f"/lite/series/{ABSENT_ULID}", True),
        (f"/lite/shelves/{ABSENT_ULID}", True),
        (f"/lite/covers/{'ab' * 32}/160", True),
        (f"/lite/editions/{ABSENT_ULID}/complete", True),
        ("/lite/works/not-an-identifier", True),
        ("/lite/works/..%2f..%2fapi", True),
    ):
        assert client.get(path).status_code == 404 and empty, path
    assert world.bundles is not None and world.bundles.opened == []  # never asked for a hidden one
    assert not set(world.bundles.offered) & {
        world.seed.editions[k] for k in ("above", "adult_edition", "misfiled", "mixed_new")
    }


def test_lite_search_finds_only_what_the_profile_may_see(world: World) -> None:
    client = signed_in(world, world.kid, "10.0.1.24")
    for key in HIDDEN_KID:
        title = world.seed.title_of(key)
        found = client.get("/lite/search", params={"q": title})
        assert found.status_code == 200
        listed = [h for h in Links(found.text).hrefs if h.startswith("/lite/works/")]
        assert listed == [], key
    seen = client.get("/lite/search", params={"q": world.seed.title_of("ok")})
    assert f"/lite/works/{world.seed.works['ok']}" in Links(seen.text).hrefs
    for hostile in ("<b>x</b>", '"><script>x</script>', "' OR 1=1 --", "a" * 5000, "%00"):
        got = client.get("/lite/search", params={"q": hostile})
        assert got.status_code in (200, 400, 404, 422)  # too long: an absent page, no search
        assert "<script" not in got.text.lower() and "<b>x</b>" not in got.text


def test_lite_follows_the_profile_at_the_next_request(world: World) -> None:
    """Nothing is kept in the cookie: a Work reclassified above her ceiling is gone for the child
    from the very next request, on a page, in a search, as a cover and as a download."""
    client = signed_in(world, world.kid, "10.0.1.25")
    work, edition = world.seed.works["adult_shelf"], world.seed.editions.get("adult_shelf")
    assert client.get(f"/lite/works/{work}").status_code == 200
    with world.seed.engine.begin() as connection:
        connection.execute(
            sa.update(catalog.work).where(catalog.work.c.id == work).values(age_rating=18)
        )
    assert client.get(f"/lite/works/{work}").status_code == 404
    assert world.seed.title_of("adult_shelf") not in client.get("/lite/works").text
    if edition is not None:
        assert client.get(f"/lite/editions/{edition}/complete").status_code == 404


def test_lite_a_profile_never_reads_another_ones_shelf(world: World) -> None:
    """IDOR: the shelf kept for another profile is an absent one to the child, and a member who
    is not named in it as well; the admin-made list of shelves holds only what is shown."""
    kid = signed_in(world, world.kid, "10.0.1.26")
    listing = kid.get("/lite/shelves")
    assert listing.status_code == 200
    assert f"/lite/shelves/{world.seed.shelves['family']}" not in listing.text
    for shelf in world.seed.shelves.values():
        if f"/lite/shelves/{shelf}" not in listing.text:
            assert kid.get(f"/lite/shelves/{shelf}").status_code == 404


# -- the download goes the way of the OPDS feed -----------------------------------------------


def test_lite_download_is_the_block_of_the_series_and_only_for_a_visible_edition(
    world: World,
) -> None:
    client = signed_in(world, world.kid, "10.0.1.27")
    work = client.get(f"/lite/works/{world.seed.works['ok']}")
    links = [h for h in Links(work.text).hrefs if "/complete" in h]
    editions = world.seed.editions  # the third Edition of this Work is on a shelf kept for others
    assert sorted(links) == sorted(
        f"/lite/editions/{editions[k]}/complete" for k in ("ok", "ok_second")
    )
    assert editions["ok_reserved"] not in work.text
    links = [f"/lite/editions/{editions['ok']}/complete"]
    assert "3 chapitres exclus" in work.text  # the notice of the provider, escaped text
    got = client.get(links[0])
    assert got.status_code == 200 and got.content == PAYLOAD
    assert got.headers["content-type"].startswith("application/vnd.comicbook+zip")
    disposition = got.headers["content-disposition"]
    assert disposition.startswith("attachment") and "/" not in disposition.split("filename=")[1]
    assert got.headers["x-content-type-options"] == "nosniff"
    assert world.bundles is not None and world.bundles.opened == [world.seed.editions["ok"]]
    assert client.get(links[0] + "?part=0").status_code in (404, 422)
    assert client.get(links[0] + "?part=1000").status_code in (404, 422)


def test_lite_download_without_a_provider_is_an_absent_thing(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with make_world(monkeypatch, settings) as bare:
        client = signed_in(bare, bare.mum, "10.0.1.28")
        page = client.get(f"/lite/works/{bare.seed.works['ok']}")
        assert page.status_code == 200 and "/complete" not in page.text
        assert client.get(f"/lite/editions/{bare.seed.editions['ok']}/complete").status_code == 404


# -- headers ------------------------------------------------------------------------------------


def test_lite_answers_are_never_cached_and_carry_a_strict_csp(world: World) -> None:
    client = browser(world, "10.0.1.29")
    shown = [client.get("/lite/sign-in"), client.get("/lite"), client.get("/lite/zz")]
    sign_in(client, world.kid)
    shown += list(crawl(client).values())
    shown.append(client.get(f"/lite/covers/{world.covers['ok']}/160"))
    shown.append(client.get(f"/lite/works/{ABSENT_ULID}"))
    assert len(shown) > 8
    for response in shown:
        assert response.headers["cache-control"] == "no-store", response.request.url
        assert "cookie" in response.headers.get("vary", "").lower(), response.request.url
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    for response in shown:
        if "text/html" in response.headers["content-type"]:
            csp = response.headers["content-security-policy"]
            for wanted in ("default-src 'none'", "form-action 'self'", "base-uri 'none'"):
                assert wanted in csp, (response.request.url, csp)


def test_lite_pages_escape_what_they_show(world: World) -> None:
    """A title is data: it is escaped in text and never reaches an attribute or a link raw."""
    evil = '"><b id=x>t</b><img src=x onerror=alert(1)>'
    with world.seed.engine.begin() as connection:
        connection.execute(
            sa.update(catalog.work)
            .where(catalog.work.c.id == world.seed.works["ok"])
            .values(title=evil)
        )
    client = signed_in(world, world.kid, "10.0.1.30")
    for path in ("/lite/works", f"/lite/works/{world.seed.works['ok']}", "/lite/recent"):
        body = client.get(path).text
        assert "<b id=x>" not in body and "<img src=x" not in body, path
        assert "&lt;b id=x&gt;" in body, path


def test_lite_is_outside_the_themes_of_the_front(world: World) -> None:
    """02 §11 bis: `/lite` has one plain stylesheet of its own, served by itself, and takes
    nothing of the front but its fonts: black on white, whatever the app is set to."""
    client = signed_in(world, world.kid, "10.0.1.31")
    body = client.get("/lite").text
    page = Links(body)
    assert "script" not in page.tags and "style" not in page.tags
    assert re.findall(r"<link[^>]*>", body) == ['<link rel="stylesheet" href="/lite/style.css">']
    assert "/_app/" not in body and "theme" not in body.lower()
    assert " style=" not in body


def test_lite_stylesheet_is_public_strictly_black_and_white_and_literal(world: World) -> None:
    """The sign-in page needs it before anyone is in; the browsers of e-readers know no custom
    property, so none is read; no colour but black and white; nothing leaves the origin."""
    client = browser(world, "10.0.1.32")
    answer = client.get("/lite/style.css")
    assert answer.status_code == 200 and answer.headers["content-type"].startswith("text/css")
    assert "frame-ancestors 'none'" in answer.headers["content-security-policy"]
    again = client.get("/lite/style.css", headers={"If-None-Match": answer.headers["etag"]})
    assert again.status_code == 304
    sheet = (
        Path(__file__).parents[2] / "src/shelfhold/interfaces/http_api/pages/lite.css"
    ).read_text("utf-8")
    assert answer.text.endswith(sheet)
    assert "var(" not in sheet and "@import" not in sheet and "url(" not in sheet
    assert not re.search(r"https?:|//", re.sub(r"/\*.*?\*/", "", sheet, flags=re.DOTALL))
    colours = {c.lower() for c in re.findall(r"#[0-9a-fA-F]{3,8}\b", sheet)}
    assert colours <= {"#000", "#fff"}
    assert "rgb" not in sheet and "hsl" not in sheet


def test_lite_pager_is_one_line_and_says_the_page(world: World) -> None:
    """Previous, the page, next: one nav, three cells of one table row. The missing side is drawn
    (dashed, `aria-disabled`), not left out, so the line keeps its shape on the last page; a list
    that fits one page has no pager."""
    client = signed_in(world, world.kid, "10.0.1.33")
    assert "lite__pager" not in client.get("/lite/works").text
    end = client.get("/lite/works", params={"offset": "100000"}).text
    nav = re.search(r'<nav class="lite__pager"[^>]*>(.*?)</nav>', end, flags=re.DOTALL)
    assert nav is not None
    cells = re.findall(r'class="(lite__cell[^"]*)"', nav[1])
    assert cells == [
        "lite__cell lite__cell--prev",
        "lite__cell lite__count",
        "lite__cell lite__cell--next",
    ]
    assert 'href="/lite/works?offset=99975"' in nav[1]  # the way back is a link
    assert 'aria-disabled="true"' in nav[1]  # the way on is drawn, not a link
    assert "Page 4001" in nav[1]
