"""M4 L48: the OPDS feed of Shelfhold behind a device token (ADR-0053 §1, §4, §8, EF-INT-007,
EF-USR-006, EF-USR-008, ENF-SEC-010).

The feed is exactly what the profile of the token may see, read from the database at each
request: a child sees nothing above her ceiling, neither an entry nor a count nor a search hit nor
a cover nor a download link, and a Work she may not see answers like a Work that does not exist.
The attacks go through the real application: a child's token, a member's, a revoked and an
expired one, no token, a web session, an API key, another profile's identifiers (IDOR on a Work,
an Edition, a cover, a shelf, a series), a search that tries to find a hidden title, a page that
tries to count one, a path that climbs out of `/opds`, and a name that tries to inject a header.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree as ET

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from tests.auth_env import AuthEnv, auth_env, run_cli
from tests.thumbnail_env import put_cover
from tests.visibility_env import HIDDEN_TITLES, VISIBLE_TITLES, Seed, add_profile, seed_catalogue

pytestmark = pytest.mark.req("EF-INT-007", "EF-USR-006", "EF-USR-008", "ENF-SEC-010")

ATOM = "{http://www.w3.org/2005/Atom}"
OS = "{http://a9.com/-/spec/opensearch/1.1/}"
ACQUISITION = "http://opds-spec.org/acquisition"
THUMBNAIL = "http://opds-spec.org/image/thumbnail"
ABSENT_ULID = "01HZZZZZZZZZZZZZZZZZZZZZZM"
ABSENT_DIGEST = "ab" * 32
PAYLOAD = b"PK\x03\x04 a block, as a fake provider streams it"
ZIP = "application/vnd.comicbook+zip"

SEEN_KID = {"ok", "adult_shelf", "mixed"}  # a ceiling of 10 (see `tests/visibility_env.py`)
HIDDEN_KID = {*HIDDEN_TITLES, "twelve"}
COVER_COLOURS = {"ok": (220, 20, 20), "above": (220, 220, 20), "mixed_new": (120, 120, 120)}


def md5(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()  # KOReader's own key


def basic(user: str, secret: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()}


@dataclass
class Reader:
    """A profile and its e-reader: its own address, its own token."""

    name: str
    client: TestClient
    public_id: str
    secret: str

    @property
    def headers(self) -> dict[str, str]:
        return basic(self.public_id, self.secret)

    def get(self, path: str, **kwargs: Any) -> Any:
        headers = {**self.headers, **kwargs.pop("headers", {})}
        return self.client.get(path, headers=headers, **kwargs)


class FakeBundles:
    """A provider that offers a block for an Edition and streams it (the contract of L49a)."""

    def __init__(self) -> None:
        self.offered: list[str] = []
        self.opened: list[str] = []
        self.filename = "Tale — Série complète v1.cbz"

    def offers(self, *, user_id: str, scope: Any, edition_id: str) -> list[Any]:
        from shelfhold.interfaces.http_api.opds.bundle import BundleOffer

        self.offered.append(edition_id)
        return [
            BundleOffer(
                filename=self.filename, size=len(PAYLOAD), media_type=ZIP,
                notice="3 chapitres exclus : format non lisible sur liseuse",
            )
        ]  # fmt: skip

    def open(self, *, user_id: str, scope: Any, edition_id: str, part: int = 1, token_id: Any = None) -> Any:
        from shelfhold.interfaces.http_api.opds.bundle import BundleDownload

        self.opened.append(edition_id)
        return BundleDownload(
            filename=self.filename, size=len(PAYLOAD), media_type=ZIP,
            chunks=iter([PAYLOAD[:7], PAYLOAD[7:]]),
        )  # fmt: skip


@dataclass
class World:
    env: AuthEnv
    seed: Seed
    kid: Reader
    mum: Reader
    kid_session: TestClient
    covers: dict[str, str]
    series: dict[str, str]
    bundles: FakeBundles | None
    _later: list[Callable[[], None]] = field(default_factory=list)

    @property
    def markers(self) -> list[str]:
        """What must never reach the child: titles and identifiers of everything hidden."""
        keys = HIDDEN_KID
        found = [self.seed.title_of(k) for k in keys] + [self.seed.works[k] for k in keys]
        found += [
            self.seed.editions[k] for k in ("above", "adult_edition", "misfiled", "mixed_new")
        ]
        found += [self.covers["above"], self.covers["mixed_new"], self.series["mixed"]]
        return found

    def clean(self, text: str) -> None:
        leaked = [m for m in self.markers if m in text]
        assert not leaked, f"a child's answer holds {leaked}"

    def work_ids(self, keys: set[str]) -> set[str]:
        return {self.seed.works[k] for k in keys}


def auth_of(env: AuthEnv) -> Any:
    return env.app.state.auth_gate.auth  # type: ignore[attr-defined]  # the app's own, its clock


def _reader(env: AuthEnv, name: str, ip: str) -> Reader:
    created = auth_of(env).create_device_token(subject_username=name, name="liseuse")
    return Reader(name, env.other_client(ip), created.public_id, created.secret)


@contextmanager
def make_world(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, bundles: FakeBundles | None = None
) -> Iterator[World]:
    if bundles is not None:
        monkeypatch.setattr("shelfhold.main.series_bundles", lambda *_a, **_k: bundles)
    with auth_env(monkeypatch, settings) as env:
        seed = seed_catalogue(env)
        kid_session = add_profile(env, "kid", "child", "10.0.0.2", max_age_rating=10)
        add_profile(env, "mum", "member", "10.0.0.3")
        assert run_cli(monkeypatch, env.settings, "search", "rebuild") == 0
        covers: dict[str, str] = {}
        series: dict[str, str] = {}
        with seed.engine.execution_options(shelfhold_begin="BEGIN IMMEDIATE").begin() as conn:
            for key, colour in COVER_COLOURS.items():
                covers[key] = put_cover(env.settings.data_dir, conn, seed.editions[key], colour)
            series["visible"] = _series(
                conn, "Orchard Run", [seed.works["ok"], seed.works["mixed"]]
            )
            series["mixed"] = _series(  # a series with one hidden member: it shows to nobody small
                conn, "Harbor Run Tale", [seed.works["ok"], seed.works["above"]]
            )
        kid = _reader(env, "kid", "10.0.0.20")
        mum = _reader(env, "mum", "10.0.0.30")
        yield World(env, seed, kid, mum, kid_session, covers, series, bundles)
        seed.engine.dispose()


def _series(connection: sa.Connection, title: str, work_ids: list[str]) -> str:
    series_id = new_ulid()
    connection.execute(
        sa.insert(catalog.series).values(id=series_id, title=title, sort_title=title.lower())
    )
    for position, work_id in enumerate(work_ids, start=1):
        connection.execute(
            sa.insert(catalog.series_member).values(
                series_id=series_id, work_id=work_id, position=str(position)
            )
        )
    return series_id


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with make_world(monkeypatch, settings) as made:
        yield made


@pytest.fixture
def bundled(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with make_world(monkeypatch, settings, FakeBundles()) as made:
        yield made


# -- reading a feed ----------------------------------------------------------------------------


def parse(response: Any) -> ET.Element:
    assert response.status_code == 200, response.text
    return ET.fromstring(response.content)  # noqa: S314


def entries(feed: ET.Element) -> list[ET.Element]:
    return feed.findall(f"{ATOM}entry")


def titles(feed: ET.Element) -> list[str]:
    return [e.findtext(f"{ATOM}title") or "" for e in entries(feed)]


def links(node: ET.Element) -> list[dict[str, str]]:
    return [dict(link.attrib) for link in node.findall(f"{ATOM}link")]


def link(node: ET.Element, rel: str) -> dict[str, str] | None:
    return next((x for x in links(node) if x.get("rel") == rel), None)


def total(feed: ET.Element) -> int | None:
    raw = feed.findtext(f"{OS}totalResults")
    return int(raw) if raw is not None else None


def work_titles(world: World, keys: set[str]) -> list[str]:
    return sorted(world.seed.title_of(k) for k in keys)


def _shape(response: Any) -> tuple[Any, ...]:
    body = response.json()
    return response.status_code, body.get("type"), body.get("title"), body.get("detail")


def crawl(reader: Reader, start: str = "/opds") -> tuple[dict[str, str], list[str]]:
    """Follows every navigation and acquisition feed from `start`: {path: body}, every href seen."""
    seen: dict[str, str] = {}
    hrefs: list[str] = []
    todo = [start]
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        response = reader.get(path)
        assert response.status_code == 200, (path, response.status_code, response.text[:200])
        seen[path] = response.text
        root = ET.fromstring(response.content)  # noqa: S314
        for node in root.iter(f"{ATOM}link"):
            href = node.attrib["href"]
            hrefs.append(href)
            kind = node.attrib.get("type", "")
            if kind.startswith("application/atom+xml") and "{" not in href:
                todo.append(href)
    return seen, hrefs


# -- the feed is exactly what the profile may see ---------------------------------------------


def test_opds_feed_respects_visibility(world: World) -> None:
    """The acceptance of EF-INT-007: a client that browses sees only the content of its scope."""
    kid = parse(world.kid.get("/opds/works"))
    assert sorted(titles(kid)) == work_titles(world, SEEN_KID)
    world.clean(ET.tostring(kid, encoding="unicode"))
    assert total(kid) == len(SEEN_KID)  # a total never counts a hidden Work
    mum = parse(world.mum.get("/opds/works?limit=50"))
    assert sorted(titles(mum)) == sorted({**VISIBLE_TITLES, **HIDDEN_TITLES}.values())
    assert total(mum) == len(VISIBLE_TITLES) + len(HIDDEN_TITLES)  # a member is not narrowed


def test_the_whole_graph_of_feeds_holds_nothing_hidden_for_a_child(world: World) -> None:
    seen, _hrefs = crawl(world.kid)
    assert len(seen) >= 6  # root, shelves, series, works, recent, a work page at least
    for body in seen.values():
        world.clean(body)
    for name in ("Tale of Harbor Sixteen", "Tale of Xylophone", "Tale of Unrated Lantern"):
        assert name not in "".join(seen.values())
    adult_seen, _ = crawl(world.mum)
    assert world.seed.title_of("above") in "".join(adult_seen.values())  # the control


def test_the_root_names_the_doors_and_a_search_and_nothing_of_the_catalogue(world: World) -> None:
    root = parse(world.kid.get("/opds"))
    assert root.findtext(f"{ATOM}title")
    rels = {x.get("rel") for x in links(root)}
    assert {"self", "start", "search"} <= rels
    kinds = {x["type"] for x in links(root)}
    assert any("kind=navigation" in k for k in kinds)
    assert not root.findall(f"{ATOM}entry/{ATOM}summary") or all(
        not any(t in (s.text or "") for t in HIDDEN_TITLES.values())
        for s in root.iter(f"{ATOM}summary")
    )


def _open_family_shelf_to_everyone(world: World) -> None:
    """The seed reserves "Family" to another profile (see test_l48_contradicteur.py); this test
    is about the ceiling, so the shelf is opened to every profile first."""
    with world.seed.engine.begin() as conn:
        conn.execute(
            sa.update(export.shelf)
            .where(export.shelf.c.id == world.seed.shelves["family"])
            .values(allowed_profiles=[])
        )


def test_a_shelf_shows_only_when_it_is_the_readers_to_see(world: World) -> None:
    _open_family_shelf_to_everyone(world)
    kid = parse(world.kid.get("/opds/shelves"))
    assert titles(kid) == ["Family"]  # the adult shelf has no ceiling: it is not a child's
    mum = parse(world.mum.get("/opds/shelves"))
    assert sorted(titles(mum)) == ["Adults", "Family"]
    shelf = world.seed.shelves["family"]
    family = parse(world.kid.get(f"/opds/shelves/{shelf}"))
    assert titles(family) == [world.seed.title_of("ok")]  # the misfiled unrated Work is not there
    assert total(family) == 1


def test_a_series_with_a_hidden_member_is_neither_listed_nor_opened_by_a_child(
    world: World,
) -> None:
    kid = parse(world.kid.get("/opds/series"))
    assert titles(kid) == ["Orchard Run"]
    inside = parse(world.kid.get(f"/opds/series/{world.series['visible']}"))
    assert sorted(titles(inside)) == work_titles(world, {"ok", "mixed"})
    mum = parse(world.mum.get("/opds/series"))
    assert sorted(titles(mum)) == ["Harbor Run Tale", "Orchard Run"]
    hidden = world.kid.get(f"/opds/series/{world.series['mixed']}")
    absent = world.kid.get(f"/opds/series/{ABSENT_ULID}")
    assert hidden.status_code == 404 and _shape(hidden) == _shape(absent)


def test_a_work_page_lists_only_the_editions_the_reader_may_see(world: World) -> None:
    kid_page = parse(world.kid.get(f"/opds/works/{world.seed.works['mixed']}"))
    assert len(entries(kid_page)) == 1  # the unrated Edition waits in "À classer"
    assert world.seed.editions["mixed_new"] not in ET.tostring(kid_page, encoding="unicode")
    mum_page = parse(world.mum.get(f"/opds/works/{world.seed.works['mixed']}"))
    assert len(entries(mum_page)) == 2


def test_the_recent_list_is_filtered_like_the_others(world: World) -> None:
    kid = parse(world.kid.get("/opds/recent"))
    assert sorted(titles(kid)) == work_titles(world, SEEN_KID)
    assert total(kid) == len(SEEN_KID)


def test_the_ceiling_lowered_applies_at_the_next_request(world: World) -> None:
    before = parse(world.kid.get("/opds/works"))
    assert world.seed.title_of("ok") in titles(before)
    with world.seed.engine.begin() as conn:  # an admin re-rates the Work above her ceiling
        conn.execute(
            sa.update(catalog.work)
            .where(catalog.work.c.id == world.seed.works["ok"])
            .values(age_rating=16)
        )
    after = parse(world.kid.get("/opds/works"))
    assert world.seed.title_of("ok") not in titles(after)
    assert total(after) == len(SEEN_KID) - 1
    gone = world.kid.get(f"/opds/works/{world.seed.works['ok']}")
    assert gone.status_code == 404


# -- pagination and search tell nothing about what is hidden -----------------------------------


def _walk(reader: Reader, first: str) -> tuple[list[str], list[int | None], int]:
    """Every title of the pages from `first`, the total each page announced, the pages count."""
    path, seen, totals, pages = first, [], [], 0
    while path is not None:
        feed = parse(reader.get(path))
        pages += 1
        assert pages < 40, "a page chain that never ends"
        seen += titles(feed)
        totals.append(total(feed))
        nxt = link(feed, "next")
        path = nxt["href"] if nxt else None
    return seen, totals, pages


def test_pages_hold_visible_rows_only_and_the_chain_ends_on_the_last_visible_one(
    world: World,
) -> None:
    seen, totals, pages = _walk(world.kid, "/opds/works?limit=1")
    assert sorted(seen) == work_titles(world, SEEN_KID)
    assert pages == len(SEEN_KID)  # no empty page where a hidden Work sits
    assert set(totals) == {len(SEEN_KID)}
    seen_all, _t, pages_all = _walk(world.mum, "/opds/works?limit=4")
    assert len(seen_all) == len(VISIBLE_TITLES) + len(HIDDEN_TITLES) and pages_all == 3


def test_the_page_after_the_last_visible_one_is_empty_and_has_no_next(world: World) -> None:
    shapes = []
    for offset in (3, 4, 50, 10**5):
        feed = parse(world.kid.get(f"/opds/works?limit=1&offset={offset}"))
        assert entries(feed) == [] and link(feed, "next") is None
        assert total(feed) == len(SEEN_KID)
        shapes.append(ET.tostring(feed, encoding="unicode").count("<entry"))
    assert set(shapes) == {0}


@pytest.mark.parametrize("query", ["limit=0", "limit=51", "limit=abc", "offset=-1", "offset=x"])
def test_a_bad_page_is_a_clean_refusal_never_a_500(world: World, query: str) -> None:
    for path in ("/opds/works", "/opds/recent", f"/opds/search?q=Tale&{query}"):
        sep = "&" if "?" in path else "?"
        url = path if "limit" in path and "q=" in path else f"{path}{sep}{query}"
        answer = world.kid.get(url)
        assert answer.status_code in (400, 422), (url, answer.status_code)
        world.clean(answer.text)


def test_the_search_finds_the_visible_and_only_the_visible(world: World) -> None:
    kid = parse(world.kid.get("/opds/search?q=Tale"))
    assert sorted(titles(kid)) == work_titles(world, SEEN_KID)
    assert total(kid) == len(SEEN_KID)
    world.clean(ET.tostring(kid, encoding="unicode"))
    mum = parse(world.mum.get("/opds/search?q=Tale&limit=50"))
    assert total(mum) == len(VISIBLE_TITLES) + len(HIDDEN_TITLES)


def test_a_search_cannot_tell_a_hidden_title_from_one_that_does_not_exist(world: World) -> None:
    nothing = parse(world.kid.get("/opds/search?q=Zzzzqx"))
    known = {
        w.lower() for k in SEEN_KID for w in world.seed.title_of(k).split()
    }  # a visible title's words
    for key in HIDDEN_KID:
        title = world.seed.title_of(key)
        own = [w for w in title.split() if not any(k.startswith(w.lower()) for k in known)]
        for query in (title, own[0]):  # the whole title, and a word only the hidden Work has
            answer = parse(world.kid.get("/opds/search", params={"q": query}))
            assert titles(answer) == titles(nothing) == [], (key, query)
            assert total(answer) == total(nothing) == 0
            assert link(answer, "next") is None
            world.clean(ET.tostring(answer, encoding="unicode").replace(quote(query), ""))
        by_id = parse(world.kid.get("/opds/search", params={"q": world.seed.works[key]}))
        assert titles(by_id) == [] and total(by_id) == 0


def test_search_pages_never_carry_a_hidden_hit_or_a_next_link_for_one(world: World) -> None:
    seen, totals, pages = _walk(world.kid, "/opds/search?q=Tale&limit=1")
    assert sorted(seen) == work_titles(world, SEEN_KID) and pages == len(SEEN_KID)
    assert set(totals) == {len(SEEN_KID)}
    # a query that only a hidden Work matches has no page at all
    empty = parse(world.kid.get("/opds/search?q=Xylophone&limit=1"))
    assert link(empty, "next") is None and total(empty) == 0


def test_the_search_is_words_never_syntax_and_empty_is_empty(world: World) -> None:
    for query in ('"', "*", "Tale OR Harbor", "Tale NOT Orchard", "title:Harbor", "a" * 500, ""):
        answer = world.kid.get("/opds/search", params={"q": query})
        assert answer.status_code in (200, 422), (query, answer.status_code)
        if answer.status_code == 200:
            world.clean(answer.text)
            assert not set(titles(parse(answer))) & {world.seed.title_of(k) for k in HIDDEN_KID}


def test_the_opensearch_description_points_under_opds(world: World) -> None:
    answer = world.kid.get("/opds/opensearch.xml")
    assert answer.status_code == 200
    assert "opensearchdescription" in answer.headers["content-type"]
    urls = re.findall(r'template="([^"]+)"', answer.text)
    assert urls and all(u.startswith("/opds/search") for u in urls)
    assert "{searchTerms}" in urls[0]


# -- IDOR: another profile's identifiers answer like an absent one -----------------------------


def test_a_hidden_work_edition_shelf_or_cover_answers_exactly_like_an_absent_one(
    world: World,
) -> None:
    absent_work = world.kid.get(f"/opds/works/{ABSENT_ULID}")
    absent_edition = world.kid.get(f"/opds/editions/{ABSENT_ULID}/complete")
    absent_shelf = world.kid.get(f"/opds/shelves/{ABSENT_ULID}")
    absent_cover = world.kid.get(f"/opds/covers/{ABSENT_DIGEST}/160")
    assert {a.status_code for a in (absent_work, absent_edition, absent_shelf, absent_cover)} == {
        404
    }
    for key in HIDDEN_KID:
        got = world.kid.get(f"/opds/works/{world.seed.works[key]}")
        assert got.status_code == 404 and _shape(got) == _shape(absent_work), key
        world.clean(got.text)
    for key in ("above", "adult_edition", "misfiled", "mixed_new"):
        got = world.kid.get(f"/opds/editions/{world.seed.editions[key]}/complete")
        assert got.status_code == 404 and _shape(got) == _shape(absent_edition), key
    adult = world.kid.get(f"/opds/shelves/{world.seed.shelves['adult']}")
    assert adult.status_code == 404 and _shape(adult) == _shape(absent_shelf)
    for key in ("above", "mixed_new"):
        got = world.kid.get(f"/opds/covers/{world.covers[key]}/160")
        assert got.status_code == 404 and _shape(got) == _shape(absent_cover), key


def test_a_cover_is_served_to_the_reader_who_may_see_it_and_to_nobody_else(world: World) -> None:
    got = world.kid.get(f"/opds/covers/{world.covers['ok']}/160")
    assert got.status_code == 200 and got.headers["content-type"] == "image/webp"
    assert "private" in got.headers["cache-control"]
    assert got.headers["x-content-type-options"] == "nosniff"
    mum = world.mum.get(f"/opds/covers/{world.covers['above']}/160")
    assert mum.status_code == 200  # the member is not narrowed
    tag = got.headers["etag"]
    stranger = world.kid.get(  # a hidden cover with the tag of a visible one: no 304, no oracle
        f"/opds/covers/{world.covers['above']}/160", headers={"If-None-Match": tag}
    )
    assert stranger.status_code == 404
    for size in ("159", "0", "-1", "big", "160.0", "1" * 30):
        assert world.kid.get(f"/opds/covers/{world.covers['ok']}/{size}").status_code == 404


def test_the_feed_links_a_cover_only_for_what_the_child_sees(world: World) -> None:
    _seen, hrefs = crawl(world.kid)
    covers = {h for h in hrefs if "/covers/" in h}
    assert any(world.covers["ok"] in c for c in covers)
    assert not any(world.covers[k] in c for k in ("above", "mixed_new") for c in covers)
    assert all(c.startswith("/opds/covers/") for c in covers)  # never the session route


def test_tokens_of_two_profiles_see_their_own_scope_each(world: World) -> None:
    kid_feed = parse(world.kid.get("/opds/works"))
    mum_feed = parse(world.mum.get("/opds/works"))
    assert set(titles(kid_feed)) < set(titles(mum_feed))
    swapped = world.kid.client.get(
        "/opds/works", headers=basic(world.mum.public_id, world.kid.secret)
    )
    assert swapped.status_code == 401  # a secret is the token's own


# -- who may come in ---------------------------------------------------------------------------

ROUTES = (
    "/opds", "/opds/opensearch.xml", "/opds/shelves", "/opds/series", "/opds/works",
    "/opds/recent", "/opds/search?q=Tale", f"/opds/works/{ABSENT_ULID}",
    f"/opds/shelves/{ABSENT_ULID}", f"/opds/series/{ABSENT_ULID}",
    f"/opds/covers/{ABSENT_DIGEST}/160", f"/opds/editions/{ABSENT_ULID}/complete",
)  # fmt: skip


def test_no_token_no_feed_and_the_answer_asks_for_basic(world: World) -> None:
    anonymous = world.env.other_client("198.51.100.7")
    for path in ROUTES:
        got = anonymous.get(path)
        assert got.status_code == 401, path
        assert "basic" in got.headers["www-authenticate"].lower()
        world.clean(got.text)


def test_unknown_wrong_revoked_and_expired_tokens_are_one_answer(world: World) -> None:
    auth = auth_of(world.env)
    gone = auth.create_device_token(subject_username="kid", name="perdue")
    old = auth.create_device_token(subject_username="kid", name="vieille", expires_days=1)
    client = world.env.other_client("198.51.100.8")
    assert client.get("/opds", headers=basic(gone.public_id, gone.secret)).status_code == 200
    assert client.get("/opds", headers=basic(old.public_id, old.secret)).status_code == 200
    assert auth.revoke_device_token(gone.public_id) is True
    world.env.clock.advance(timedelta(days=2))
    answers = [
        client.get("/opds", headers=basic("zzzzzzzz", "x" * 26)),  # unknown
        client.get("/opds", headers=basic(world.kid.public_id, "x" * 26)),  # wrong
        client.get("/opds", headers=basic(gone.public_id, gone.secret)),  # revoked
        client.get("/opds", headers=basic(old.public_id, old.secret)),  # expired
    ]
    assert {a.status_code for a in answers} == {401}
    first = answers[0].json()
    first.pop("request_id", None)
    for other in answers[1:]:
        body = other.json()
        body.pop("request_id", None)
        assert body == first


def test_a_revoked_token_stops_the_very_next_page_of_a_walk(world: World) -> None:
    auth = auth_of(world.env)
    created = auth.create_device_token(subject_username="kid", name="salon")
    client = world.env.other_client("198.51.100.9")
    headers = basic(created.public_id, created.secret)
    first = client.get("/opds/works?limit=1", headers=headers)
    assert first.status_code == 200
    next_href = link(parse(first), "next")
    assert next_href is not None
    assert auth.revoke_device_token(created.public_id) is True
    assert client.get(next_href["href"], headers=headers).status_code == 401


def test_a_web_session_opens_nothing_under_opds(world: World) -> None:
    admin = world.env.client  # logged in as the admin by `auth_env`
    for session in (world.kid_session, admin):
        for path in ROUTES:
            got = session.get(path)
            assert got.status_code == 401, (path, got.status_code)
            world.clean(got.text)


def test_an_account_password_is_not_a_device_secret(world: World) -> None:
    from tests.auth_env import PASSWORD

    got = world.kid.client.get("/opds", headers=basic("kid", PASSWORD))
    assert got.status_code == 401


def test_an_api_key_opens_nothing_under_opds(world: World) -> None:
    auth = auth_of(world.env)
    key = auth.create_api_key(name="k", username="kid", read_only=True).value
    client = world.env.other_client("198.51.100.10")
    assert client.get("/opds", headers={"Authorization": f"Bearer {key}"}).status_code == 401
    assert client.get("/opds", headers=basic("kid", key)).status_code == 401


def test_a_device_token_opens_nothing_outside_opds_and_kosync(world: World) -> None:
    for path in ("/api/v1/works", "/api/v1/search?q=Tale", "/api/v1/shelves", "/api/v1/me",
                 f"/api/v1/thumbnails/{world.covers['ok']}/160"):  # fmt: skip
        for reader in (world.kid, world.mum):
            got = reader.get(path)
            assert got.status_code in (401, 403), (path, got.status_code)
            world.clean(got.text)


def test_the_md5_of_the_secret_does_not_open_the_feed(world: World) -> None:
    got = world.kid.client.get("/opds", headers=basic(world.kid.public_id, md5(world.kid.secret)))
    assert got.status_code == 401  # kosync takes the md5, the OPDS door takes the secret


def test_wrong_secrets_over_the_limit_make_the_pair_wait_on_opds(world: World) -> None:
    client = world.env.other_client("198.51.100.11")
    codes = [
        client.get("/opds", headers=basic(world.kid.public_id, f"{i}" * 26)).status_code
        for i in range(8)
    ]
    assert codes[:5] == [401] * 5 and set(codes[5:]) == {429}
    assert client.get("/opds", headers=world.kid.headers).status_code == 429  # the pair waits
    elsewhere = world.env.other_client("198.51.100.12")  # the reader's own address is another pair
    assert elsewhere.get("/opds", headers=world.kid.headers).status_code == 200


# -- TLS ---------------------------------------------------------------------------------------


def test_plain_http_is_refused_before_the_secret_is_looked_at(world: World) -> None:
    plain = world.env.other_client("192.0.2.40", base_url="http://testserver")
    for path in ROUTES:
        right = plain.get(path, headers=world.kid.headers)
        wrong = plain.get(path, headers=basic(world.kid.public_id, "n" * 26))
        assert right.status_code == wrong.status_code == 403, path
        assert right.json()["code"] == wrong.json()["code"] == "device_tls_required"
        world.clean(right.text)
    secure = world.env.other_client("192.0.2.40")  # nothing was counted against the reader
    assert secure.get("/opds", headers=world.kid.headers).status_code == 200


# -- the links: under /opds, the mount point, the trusted proxy, no secret ---------------------


def test_every_link_stays_under_opds_and_carries_no_secret_or_host(world: World) -> None:
    for reader in (world.kid, world.mum):
        _seen, hrefs = crawl(reader)
        assert hrefs
        for href in hrefs:
            parts = urlsplit(href)
            assert not parts.scheme and not parts.netloc, href  # never built from `Host`
            assert parts.path.startswith("/opds"), href
            assert "@" not in href and ".." not in href, href
            assert reader.secret not in href and md5(reader.secret) not in href
            assert reader.public_id not in href
            assert "token" not in href.lower() and "key" not in href.lower().replace("keyword", "")
    for secret in (world.kid.secret, world.kid.public_id):
        assert secret not in "".join(crawl(world.kid)[0].values())


def test_a_forged_host_header_changes_no_link(world: World) -> None:
    plain = parse(world.kid.get("/opds"))
    forged = parse(
        world.kid.get("/opds", headers={"Host": "evil.example", "X-Forwarded-Host": "e.x"})
    )
    assert [x["href"] for x in links(plain)] == [x["href"] for x in links(forged)]
    assert "evil.example" not in ET.tostring(forged, encoding="unicode")


def test_links_follow_the_root_path_and_the_device_door_still_opens(world: World) -> None:
    mounted = TestClient(world.env.app, base_url="https://testserver", client=("10.0.0.40", 4000),
                         root_path="/shelf")  # fmt: skip
    for path in ("/shelf/opds", "/opds"):  # a server that keeps the prefix in `path`, or not
        got = mounted.get(path, headers=world.kid.headers)
        assert got.status_code == 200, path
        root = ET.fromstring(got.content)  # noqa: S314
        hrefs = [x["href"] for x in links(root)]
        assert hrefs and all(h.startswith("/shelf/opds") for h in hrefs), hrefs
    deep = mounted.get("/shelf/opds/works?limit=1", headers=world.kid.headers)
    assert deep.status_code == 200
    assert link(ET.fromstring(deep.content), "next")["href"].startswith("/shelf/opds/works")  # type: ignore[index] # noqa: S314
    outside = mounted.get("/shelf/api/v1/profiles", headers=world.kid.headers)
    assert outside.status_code in (401, 403, 404)  # the closed list holds under a prefix too


def test_the_forwarded_prefix_counts_behind_a_trusted_proxy_only(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    trusted = settings.model_copy(update={"http_trusted_proxies": ("192.0.2.0/24",)})
    with make_world(monkeypatch, trusted) as w:
        said = {**w.kid.headers, "X-Forwarded-Prefix": "/books", "X-Forwarded-Proto": "https"}
        proxy = w.env.other_client("192.0.2.43", base_url="http://testserver")
        root = ET.fromstring(proxy.get("/opds", headers=said).content)  # noqa: S314
        assert all(x["href"].startswith("/books/opds") for x in links(root))
        evil = {**said, "X-Forwarded-Prefix": "//evil.example"}
        root = ET.fromstring(proxy.get("/opds", headers=evil).content)  # noqa: S314
        assert all(x["href"].startswith("/opds") for x in links(root))
        stranger = w.env.other_client("198.51.100.44")  # not a proxy: the header is not read
        root = ET.fromstring(  # noqa: S314
            stranger.get("/opds", headers={**w.kid.headers, "X-Forwarded-Prefix": "/books"}).content
        )
        assert all(x["href"].startswith("/opds") for x in links(root))


def test_headers_keep_a_private_feed_private(world: World) -> None:
    got = world.kid.get("/opds/works")
    assert "private" in got.headers["cache-control"]
    assert "authorization" in got.headers["vary"].lower()
    assert got.headers["content-type"].startswith("application/atom+xml")
    assert "profile=opds-catalog" in got.headers["content-type"]
    assert "kind=acquisition" in got.headers["content-type"]
    nav = world.kid.get("/opds")
    assert "kind=navigation" in nav.headers["content-type"]


def test_a_feed_reads_no_secret_and_no_title_in_logs(
    world: World, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(0)
    crawl(world.kid)
    world.kid.get("/opds/search", params={"q": "Tale of Orchard"})
    world.kid.client.get("/opds", headers=basic(world.kid.public_id, "w" * 26))
    seen = capsys.readouterr()
    everything = seen.out + seen.err + caplog.text
    wrong = base64.b64encode(f"{world.kid.public_id}:{'w' * 26}".encode()).decode()
    for secret in (world.kid.secret, md5(world.kid.secret), "w" * 26, wrong,
                   world.kid.headers["Authorization"]):  # fmt: skip
        assert secret not in everything
    assert "Tale of Orchard" not in everything  # the title typed in a search is not logged


# -- the extension point of the block (L49a): the route checks, the provider serves ------------


def test_without_a_provider_an_edition_offers_no_download(world: World) -> None:
    page = parse(world.kid.get(f"/opds/works/{world.seed.works['ok']}"))
    assert entries(page), "the edition is listed"
    assert not [x for x in links(page) if x.get("rel") == ACQUISITION]
    for entry in entries(page):
        assert link(entry, ACQUISITION) is None
    got = world.kid.get(f"/opds/editions/{world.seed.editions['ok']}/complete")
    assert got.status_code == 404  # nothing to serve is the answer of an absent thing


def test_a_provider_offer_is_linked_for_a_visible_edition_and_served(bundled: World) -> None:
    fake = bundled.bundles
    assert fake is not None
    page = parse(bundled.kid.get(f"/opds/works/{bundled.seed.works['ok']}"))
    assert len(entries(page)) == 2
    for entry in entries(page):
        acquisition = link(entry, ACQUISITION)
        assert acquisition is not None
        assert acquisition["type"] == ZIP and acquisition["length"] == str(len(PAYLOAD))
        assert re.fullmatch(r"/opds/editions/[0-9A-Z]{26}/complete", acquisition["href"])
        assert "3 chapitres exclus" in ET.tostring(entry, encoding="unicode")
    assert set(fake.offered) <= {bundled.seed.editions["ok"], bundled.seed.editions["ok_second"]}
    href = link(entries(page)[0], ACQUISITION)["href"]  # type: ignore[index]
    got = bundled.kid.get(href)
    assert got.status_code == 200 and got.content == PAYLOAD
    assert got.headers["content-length"] == str(len(PAYLOAD))
    assert got.headers["content-type"] == ZIP
    assert got.headers["content-disposition"].startswith("attachment;")
    assert got.headers["x-content-type-options"] == "nosniff"
    assert "private" in got.headers["cache-control"]


def test_a_hidden_edition_is_never_offered_nor_opened_for_a_child(bundled: World) -> None:
    fake = bundled.bundles
    assert fake is not None
    crawl(bundled.kid)
    hidden = {bundled.seed.editions[k] for k in ("above", "adult_edition", "misfiled", "mixed_new")}
    assert not hidden & set(fake.offered)  # the provider is not even asked
    for edition_id in hidden:
        assert bundled.kid.get(f"/opds/editions/{edition_id}/complete").status_code == 404
    assert not hidden & set(fake.opened)
    mine = bundled.mum.get(f"/opds/editions/{bundled.seed.editions['mixed_new']}/complete")
    assert mine.status_code == 200  # the member is not narrowed


def test_the_visibility_of_a_download_is_read_at_the_request_not_at_the_feed(
    bundled: World,
) -> None:
    fake = bundled.bundles
    assert fake is not None
    edition_id = bundled.seed.editions["ok_second"]
    assert bundled.kid.get(f"/opds/editions/{edition_id}/complete").status_code == 200
    with bundled.seed.engine.begin() as conn:  # re-rated above her ceiling since the feed was read
        conn.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == edition_id)
            .values(age_rating=18)
        )
    fake.opened.clear()
    assert bundled.kid.get(f"/opds/editions/{edition_id}/complete").status_code == 404
    assert fake.opened == []


@pytest.mark.parametrize(
    "ident",
    [
        "..%2f..%2fetc%2fpasswd",
        "%2e%2e",
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "..;",
        "01HZZZZZZZZZZZZZZZZZZZZZZM%00",
        "01hzzzzzzzzzzzzzzzzzzzzzzm",
        "a" * 5000,
        "01HZZZZZZZZZZZZZZZZZZZZZZM%0d%0aX-Injected:%201",
        "%252e%252e",
        "\\..\\..\\x",
    ],
)
def test_a_path_that_climbs_reaches_neither_the_provider_nor_the_disk(
    bundled: World, ident: str
) -> None:
    fake = bundled.bundles
    assert fake is not None
    for path in (f"/opds/editions/{ident}/complete", f"/opds/works/{ident}",
                 f"/opds/covers/{ident}/160", f"/opds/shelves/{ident}",
                 f"/opds/series/{ident}"):  # fmt: skip
        got = bundled.kid.get(path)
        assert got.status_code in (404, 400, 422), (path, got.status_code)
        assert "root:" not in got.text and "x-injected" not in {k.lower() for k in got.headers}
    assert fake.opened == []


def test_a_file_name_from_a_provider_cannot_climb_or_inject_a_header(bundled: World) -> None:
    fake = bundled.bundles
    assert fake is not None
    for hostile in ("../../etc/passwd", "a\r\nSet-Cookie: s=1", 'x";filename="../../y', "..\\w"):
        fake.filename = hostile
        got = bundled.kid.get(f"/opds/editions/{bundled.seed.editions['ok']}/complete")
        assert got.status_code == 200
        disposition = got.headers["content-disposition"]
        assert "set-cookie" not in {k.lower() for k in got.headers}
        assert "../" not in disposition and "..\\" not in disposition
        assert "\r" not in disposition and "\n" not in disposition
        name = re.search(r'filename="([^"]*)"', disposition)
        assert name and "/" not in name.group(1) and "\\" not in name.group(1)
    page = parse(bundled.kid.get(f"/opds/works/{bundled.seed.works['ok']}"))
    assert all("../" not in (t or "") for t in titles(page))


def test_a_block_is_streamed_whole_only_to_the_token_that_asked_with_a_good_secret(
    bundled: World,
) -> None:
    path = f"/opds/editions/{bundled.seed.editions['ok']}/complete"
    anonymous = bundled.env.other_client("198.51.100.12")
    assert anonymous.get(path).status_code == 401
    assert bundled.kid_session.get(path).status_code == 401
    assert (
        bundled.kid.client.get(path, headers=basic(bundled.kid.public_id, "n" * 26)).status_code
        == 401
    )
    assert bundled.bundles is not None and bundled.bundles.opened == []
