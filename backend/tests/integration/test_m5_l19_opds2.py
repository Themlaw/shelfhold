"""M5 L19 (EF-INT-007, EF-USR-006, ENF-SEC-010, ENF-SEC-013): the OPDS 2.0 JSON feed, `/opds/v2`.

The JSON feed is the same feed as the Atom one, written another way: the same queries (the one
filter of the catalogue, read from the profile of the token at every request), the same tokens
(HTTP Basic, a device token only), the same answers for what is hidden and what does not exist.
It adds no entry the 1.2 feed does not hold, and omits none that it does. The proof is a
meta-test that walks both graphs for an adult, a child and a key, and compares them page by page.

The attacks go through the real application: a child's token, a member's, a revoked one, no
token, a web session, an API key, another profile's identifiers (IDOR on a Work, a shelf, a
series), a search that tries to find a hidden title, malformed identifiers, a path that climbs.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.integration.test_l48_opds import (  # noqa: F401  (fixtures)
    ABSENT_ULID,
    HIDDEN_KID,
    SEEN_KID,
    FakeBundles,
    Reader,
    World,
    _shape,
    auth_of,
    basic,
    bundled,
    crawl,
    parse,
    work_titles,
    world,
)
from tests.integration.test_m5_l18_pse import Pse, png_pages, pse  # noqa: F401  (fixture)

pytestmark = pytest.mark.req("EF-INT-007", "EF-USR-006", "ENF-SEC-010")

JSON_TYPE = "application/opds+json"
V2 = "/opds/v2"
ROUTES = (
    V2, f"{V2}/shelves", f"{V2}/series", f"{V2}/works", f"{V2}/recent", f"{V2}/search?q=Tale",
    f"{V2}/works/{ABSENT_ULID}", f"{V2}/shelves/{ABSENT_ULID}", f"{V2}/series/{ABSENT_ULID}",
)  # fmt: skip


# -- reading a JSON feed -----------------------------------------------------------------------


def doc(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].split(";")[0] == JSON_TYPE
    return json.loads(response.content)


def publications(feed: dict[str, Any]) -> list[dict[str, Any]]:
    return feed.get("publications", [])


def nav(feed: dict[str, Any]) -> list[dict[str, Any]]:
    return feed.get("navigation", [])


def names(feed: dict[str, Any]) -> list[str]:
    found = [p["metadata"]["title"] for p in publications(feed)]
    return found + [n["title"] for n in nav(feed)]


def rel_link(feed: dict[str, Any], rel: str) -> dict[str, Any] | None:
    return next((x for x in feed.get("links", []) if x.get("rel") == rel), None)


def crawl2(reader: Reader, start: str = V2) -> dict[str, dict[str, Any]]:
    """Follows every feed link from `start`: {path: document}."""
    seen: dict[str, dict[str, Any]] = {}
    todo = [start]
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen[path] = doc(reader.get(path))
        todo += [x["href"] for x in _feed_links(seen[path]) if "{" not in x["href"]]
    return seen


def _feed_links(feed: dict[str, Any]) -> list[dict[str, Any]]:
    found = list(feed.get("links", [])) + nav(feed)
    for pub in publications(feed):
        found += pub.get("links", [])
    return [x for x in found if x.get("type", "").startswith(JSON_TYPE)]


def all_hrefs(feed: dict[str, Any]) -> list[str]:
    found = [x["href"] for x in feed.get("links", [])] + [x["href"] for x in nav(feed)]
    for pub in publications(feed):
        found += [x["href"] for x in pub.get("links", []) + pub.get("images", [])]
    return found


# -- the acceptance: a child sees only what her ceiling lets through ---------------------------


def test_opds2_respects_visibility(world: World) -> None:
    kid = doc(world.kid.get(f"{V2}/works"))
    assert sorted(p["metadata"]["title"] for p in publications(kid)) == work_titles(world, SEEN_KID)
    world.clean(json.dumps(kid))
    assert kid["metadata"]["numberOfItems"] == len(SEEN_KID)  # a total never counts a hidden Work
    mum = doc(world.mum.get(f"{V2}/works?limit=50"))
    assert mum["metadata"]["numberOfItems"] > len(SEEN_KID)  # a member is not narrowed
    assert world.seed.title_of("above") in names(mum)  # the control
    seen = crawl2(world.kid)
    assert len(seen) >= 6
    for body in seen.values():
        world.clean(json.dumps(body))
    for key in HIDDEN_KID:
        assert world.seed.title_of(key) not in "".join(json.dumps(b) for b in seen.values())


def test_opds2_a_hidden_thing_is_the_answer_of_an_absent_one(world: World) -> None:
    kid = world.kid
    mixed = world.series["mixed"]
    shelf = world.seed.shelves["adult"]
    absent = {
        "works": kid.get(f"{V2}/works/{ABSENT_ULID}"),
        "series": kid.get(f"{V2}/series/{ABSENT_ULID}"),
        "shelves": kid.get(f"{V2}/shelves/{ABSENT_ULID}"),
    }
    assert {a.status_code for a in absent.values()} == {404}
    for key in HIDDEN_KID:
        hidden = kid.get(f"{V2}/works/{world.seed.works[key]}")
        assert _shape(hidden) == _shape(absent["works"]), key
        assert "application/problem+json" in hidden.headers["content-type"]
        old = kid.get(f"/opds/works/{world.seed.works[key]}")
        assert _shape(hidden) == _shape(old), key  # the same 404 as the 1.2 feed
        found = kid.get(f"{V2}/search", params={"q": world.seed.title_of(key)})
        assert publications(doc(found)) == [], key
    assert _shape(kid.get(f"{V2}/series/{mixed}")) == _shape(absent["series"])
    assert _shape(kid.get(f"{V2}/shelves/{shelf}")) == _shape(absent["shelves"])
    assert world.mum.get(f"{V2}/works/{world.seed.works['above']}").status_code == 200  # control
    for bad in ("not-an-id", "%2e%2e", "01HZ", "x%2fworks", ABSENT_ULID.lower()):
        got = kid.get(f"{V2}/works/{bad}")
        assert got.status_code in (404, 405), bad
        assert got.status_code != 200


def test_opds2_search_echoes_nothing_it_did_not_find(world: World) -> None:
    hidden = world.seed.title_of("above")
    feed = doc(world.kid.get(f"{V2}/search", params={"q": hidden}))
    assert publications(feed) == [] and feed["metadata"]["numberOfItems"] == 0
    assert "metadata" in feed and hidden not in json.dumps(feed["metadata"])
    mum = doc(world.mum.get(f"{V2}/search", params={"q": hidden}))
    assert hidden in names(mum)  # the control: an adult finds it


def test_opds2_a_next_link_exists_only_when_a_visible_row_follows(world: World) -> None:
    last = doc(world.kid.get(f"{V2}/works", params={"limit": 1, "offset": 2}))
    assert rel_link(last, "next") is None and len(publications(last)) == 1  # three are visible
    beyond = doc(world.kid.get(f"{V2}/works", params={"limit": 1, "offset": 3}))
    assert publications(beyond) == [] and rel_link(beyond, "next") is None
    first = doc(world.kid.get(f"{V2}/works", params={"limit": 1}))
    assert rel_link(first, "next") is not None and rel_link(first, "previous") is None
    assert first["metadata"]["itemsPerPage"] == 1 and first["metadata"]["currentPage"] == 1


# -- the meta-test: every 2.0 entry has its 1.2 equivalent, and the other way round --------------


def _v1_path(path: str) -> str:
    return path.replace("/opds/v2", "/opds", 1)


def _entries_1(body: str) -> dict[str, tuple[str, list[str]]]:
    from xml.etree import ElementTree as ET

    atom = "{http://www.w3.org/2005/Atom}"
    found = {}
    for entry in ET.fromstring(body).findall(f"{atom}entry"):  # noqa: S314
        hrefs = sorted(_v1_path(x.attrib["href"]) for x in entry.findall(f"{atom}link"))
        found[entry.findtext(f"{atom}id") or ""] = (entry.findtext(f"{atom}title") or "", hrefs)
    return found


def _entries_2(feed: dict[str, Any]) -> dict[str, tuple[str, list[str]]]:
    found = {}
    for pub in publications(feed):
        hrefs = [x["href"] for x in pub.get("links", []) + pub.get("images", [])]
        found[pub["metadata"]["identifier"]] = (
            pub["metadata"]["title"],
            sorted(_v1_path(h) for h in hrefs),
        )
    for item in nav(feed):
        found[item["properties"]["identifier"]] = (item["title"], [_v1_path(item["href"])])
    return found


def _total_1(body: str) -> int | None:
    from xml.etree import ElementTree as ET

    raw = ET.fromstring(body).findtext("{http://a9.com/-/spec/opensearch/1.1/}totalResults")  # noqa: S314
    return int(raw) if raw is not None else None


def _same_feeds(one: Reader, label: str) -> None:
    atom, _hrefs = crawl(one)
    json_feeds = crawl2(one)
    assert {_v1_path(p) for p in json_feeds} == set(atom), label  # the same graph of pages
    for path, feed in json_feeds.items():
        body = atom[_v1_path(path)]
        left, right = _entries_1(body), _entries_2(feed)
        assert right.keys() == left.keys(), (label, path)  # none missing, none added
        for key in left:
            assert right[key][0] == left[key][0], (label, path, key)  # the same title
            wanted = [h for h in left[key][1] if "{pageNumber}" not in h]
            got = [h for h in right[key][1] if "{pageNumber}" not in h]
            assert got == wanted, (label, path, key)  # the same links (feeds mapped, files equal)
        total = _total_1(body)
        if total is not None:
            assert feed["metadata"]["numberOfItems"] == total, (label, path)


def test_opds2_feed_matches_opds1_entries(bundled: World) -> None:
    """The meta-test of L19: for an adult and for a child, the two graphs are the same pages and
    the same entries, with the same titles, links, totals; for a key, both are refused."""
    _same_feeds(bundled.kid, "child")
    _same_feeds(bundled.mum, "adult")
    kid_pages, mum_pages = crawl(bundled.kid)[0], crawl(bundled.mum)[0]
    assert len(mum_pages) > len(kid_pages) - 1  # the adult sees at least what the child sees
    key = auth_of(bundled.env).create_api_key(name="k", username="mum", read_only=True).value
    client = bundled.env.other_client("198.51.100.30")
    for headers in ({"Authorization": f"Bearer {key}"}, basic("mum", key)):
        one, two = (
            client.get("/opds/works", headers=headers),
            client.get(f"{V2}/works", headers=headers),
        )
        assert one.status_code == two.status_code == 401


def test_opds2_the_acquisition_and_the_notice_are_those_of_opds1(bundled: World) -> None:
    work = bundled.seed.works["ok"]
    feed = doc(bundled.mum.get(f"{V2}/works/{work}"))
    pubs = publications(feed)
    assert pubs
    acquisition = [
        x for p in pubs for x in p["links"] if x["rel"] == "http://opds-spec.org/acquisition"
    ]
    assert acquisition
    for link in acquisition:
        assert link["href"].startswith("/opds/editions/")  # the file route is the one of 1.2
        assert link["type"] == "application/vnd.comicbook+zip" and link["length"] > 0
        assert bundled.mum.get(link["href"]).status_code == 200
    assert any("exclus" in p["metadata"].get("description", "") for p in pubs)  # the notice


def test_opds2_a_hidden_edition_is_offered_to_nobody_and_never_asked_of_the_provider(
    bundled: World,
) -> None:
    assert bundled.bundles is not None
    doc(bundled.kid.get(f"{V2}/works/{bundled.seed.works['ok']}"))
    asked = set(bundled.bundles.offered)
    assert (
        bundled.seed.editions["above"] not in asked
        and bundled.seed.editions["adult_edition"] not in asked
    )
    hidden = bundled.kid.get(f"{V2}/works/{bundled.seed.works['above']}")
    assert hidden.status_code == 404
    assert bundled.seed.editions["above"] not in set(bundled.bundles.offered)


def test_opds2_pse_stream_is_a_templated_link_of_a_visible_volume(pse: Pse) -> None:
    item = pse.stream("ok", png_pages(3))
    hidden = pse.stream("above", png_pages(2))
    feed = doc(pse.world.kid.get(f"{V2}/works/{pse.world.seed.works['ok']}"))
    streams = [
        x for p in publications(feed) for x in p["links"]
        if x["rel"] == "http://vaemendis.net/opds-pse/stream"
    ]  # fmt: skip
    (found,) = streams
    assert found["templated"] is True and found["properties"]["numberOfItems"] == 3
    assert found["href"] == f"/opds/pse/{item}/{{pageNumber}}"  # the page route is the one of 1.2
    assert hidden not in json.dumps(feed)
    assert pse.page(pse.world.kid, item, 0).status_code == 200
    assert pse.page(pse.world.kid, hidden, 0).status_code == 404
    assert doc(pse.world.mum.get(f"{V2}/works/{pse.world.seed.works['above']}"))  # the control


# -- the tokens: the same door as the 1.2 feed -------------------------------------------------


def test_opds2_no_token_no_feed_and_the_answer_asks_for_basic(world: World) -> None:
    anonymous = world.env.other_client("198.51.100.31")
    for path in ROUTES:
        got = anonymous.get(path)
        assert got.status_code == 401, path
        assert "basic" in got.headers["www-authenticate"].lower()
        world.clean(got.text)


def test_opds2_a_session_a_key_and_a_wrong_secret_open_nothing(world: World) -> None:
    for session in (world.kid_session, world.env.client):
        for path in ROUTES:
            assert session.get(path).status_code == 401, path
    key = auth_of(world.env).create_api_key(name="k", username="kid", read_only=True).value
    client = world.env.other_client("198.51.100.32")
    assert client.get(V2, headers={"Authorization": f"Bearer {key}"}).status_code == 401
    assert client.get(V2, headers=basic("kid", key)).status_code == 401
    assert client.get(V2, headers=basic(world.kid.public_id, "x" * 26)).status_code == 401
    assert client.get(V2, headers=world.kid.headers).status_code == 200  # the control


def test_opds2_a_revoked_token_stops_the_very_next_page(world: World) -> None:
    auth = auth_of(world.env)
    created = auth.create_device_token(subject_username="kid", name="salon")
    client = world.env.other_client("198.51.100.33")
    headers = basic(created.public_id, created.secret)
    first = client.get(f"{V2}/works?limit=1", headers=headers)
    nxt = rel_link(doc(first), "next")
    assert nxt is not None
    assert auth.revoke_device_token(created.public_id) is True
    assert client.get(nxt["href"], headers=headers).status_code == 401


def test_opds2_plain_http_is_refused_before_the_secret_is_looked_at(world: World) -> None:
    plain = world.env.other_client("192.0.2.41", base_url="http://testserver")
    for path in ROUTES:
        right = plain.get(path, headers=world.kid.headers)
        wrong = plain.get(path, headers=basic(world.kid.public_id, "n" * 26))
        assert right.status_code == wrong.status_code == 403, path
        assert right.json()["code"] == "device_tls_required"


def test_opds2_a_device_token_of_a_child_reads_as_that_child_for_a_member_too(
    world: World,
) -> None:
    mum = doc(world.mum.get(f"{V2}/works?limit=50"))
    kid = doc(world.kid.get(f"{V2}/works?limit=50"))
    assert {p["metadata"]["identifier"] for p in publications(kid)} < {
        p["metadata"]["identifier"] for p in publications(mum)
    }


# -- the links: under /opds, no host, no secret; the answers are not cached --------------------


def test_opds2_every_link_stays_under_opds_and_carries_no_secret_or_host(world: World) -> None:
    from urllib.parse import urlsplit

    for reader in (world.kid, world.mum):
        hrefs = [h for feed in crawl2(reader).values() for h in all_hrefs(feed)]
        assert hrefs
        for href in hrefs:
            parts = urlsplit(href)
            assert not parts.scheme and not parts.netloc, href
            assert parts.path.startswith("/opds"), href
            assert "@" not in href and ".." not in href, href
            assert reader.secret not in href and reader.public_id not in href
    root = doc(world.kid.get(V2))
    assert rel_link(root, "self")["href"] == V2  # type: ignore[index]
    search = rel_link(root, "search")
    assert search is not None and search["templated"] is True and search["href"].startswith(V2)


def test_opds2_headers_are_those_of_the_atom_feed(world: World) -> None:
    for path in (V2, f"{V2}/works", f"{V2}/search?q=Tale"):
        got = world.kid.get(path)
        one = world.kid.get(_v1_path(path))
        for header in ("cache-control", "vary", "x-content-type-options"):
            assert got.headers.get(header) == one.headers.get(header), (path, header)
    problem = world.kid.get(f"{V2}/works/{ABSENT_ULID}")
    assert problem.headers["cache-control"] == "no-store"


def test_opds2_a_forged_host_header_changes_no_link(world: World) -> None:
    plain = doc(world.kid.get(V2))
    forged = doc(world.kid.get(V2, headers={"Host": "evil.example", "X-Forwarded-Host": "e.x"}))
    assert plain == forged and "evil.example" not in json.dumps(forged)


def test_opds2_hostile_text_is_data(world: World) -> None:
    """A title is escaped by the JSON writer, never concatenated: the document always parses."""
    feed = doc(world.kid.get(f"{V2}/search", params={"q": '"}],"x":[{"'}))
    assert set(feed) <= {"metadata", "links", "navigation", "publications", "facets", "groups"}
