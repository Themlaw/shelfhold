"""PR #392 (M5 L19), the contradicteur: what the meta-test of L19 does not compare yet.

The meta-test walks both graphs from their roots and compares the entries, their titles, their
hrefs and the totals. These probes push where a crawl does not go: every page of every list at
odd limits and offsets (beyond the last row too), the fields of an entry the meta-test leaves out
(the notice, the media type and the length of a file, the `next` and `previous` links), the 404
and 422 bodies of both feeds, and the client fallback that answers an unknown `/opds/...` path.
"""

# ruff: noqa: F811  (the fixtures of the L48 bench are imported to be reused)

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pytest

from shelfhold.config import Settings
from tests.integration.test_l48_opds import (  # noqa: F401  (fixtures)
    ABSENT_ULID,
    HIDDEN_KID,
    SEEN_KID,
    FakeBundles,
    Reader,
    World,
    bundled,
    make_world,
    world,
)

pytestmark = pytest.mark.req("EF-INT-007", "EF-USR-006", "ENF-SEC-010")

ATOM = "{http://www.w3.org/2005/Atom}"
SEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
V2 = "/opds/v2"


def _v1(path: str) -> str:
    return path.replace("/opds/v2", "/opds", 1)


def _atom_page(reader: Reader, path: str) -> dict[str, Any]:
    got = reader.get(path)
    assert got.status_code == 200, (path, got.text)
    root = ET.fromstring(got.text)  # noqa: S314  (our own feed, in a test)
    links = {x.attrib["rel"]: x.attrib["href"] for x in root.findall(f"{ATOM}link")}
    entries = []
    for entry in root.findall(f"{ATOM}entry"):
        entries.append(
            (
                entry.findtext(f"{ATOM}id"),
                entry.findtext(f"{ATOM}summary"),
                sorted(
                    (x.attrib["rel"], x.attrib.get("type"), x.attrib.get("length"))
                    for x in entry.findall(f"{ATOM}link")
                    if "image" not in x.attrib["rel"]
                ),
            )
        )
    total = root.findtext(f"{SEARCH}totalResults")
    return {
        "ids": [e[0] for e in entries],
        "entries": entries,
        "total": int(total) if total is not None else None,
        "next": links.get("next"),
        "previous": links.get("previous"),
    }


def _json_page(reader: Reader, path: str) -> dict[str, Any]:
    got = reader.get(path)
    assert got.status_code == 200, (path, got.text)
    feed = json.loads(got.content)
    links = {x["rel"]: x["href"] for x in feed.get("links", [])}
    entries = []
    for pub in feed.get("publications", []):
        entries.append(
            (
                pub["metadata"]["identifier"],
                pub["metadata"].get("description"),
                sorted(
                    (x["rel"], x.get("type"), str(x["length"]) if "length" in x else None)
                    for x in pub.get("links", [])
                ),
            )
        )
    for item in feed.get("navigation", []):
        entries.append((item["properties"]["identifier"], None, [(item["rel"], None, None)]))
    return {
        "ids": [e[0] for e in entries],
        "entries": entries,
        "total": feed["metadata"].get("numberOfItems"),
        "next": _v1(links["next"]) if "next" in links else None,
        "previous": _v1(links["previous"]) if "previous" in links else None,
    }


def _lists(w: World) -> list[str]:
    return [
        "/works",
        "/recent",
        "/series",
        f"/series/{w.series['visible']}",
        "/search?q=Tale",
        "/search?q=Run",
    ]


@pytest.mark.parametrize("who", ["kid", "mum"])
def test_rev392_every_page_at_every_offset_is_the_same_rows_total_and_links(
    world: World, who: str
) -> None:
    """A page beyond the last row, an offset that is not a multiple of the limit: the same rows,
    the same total, the same `next` and `previous` in both feeds, for a child and an adult."""
    reader: Reader = getattr(world, who)
    for base in _lists(world):
        sep = "&" if "?" in base else "?"
        for limit in (1, 2, 3):
            for offset in (0, 1, 2, 3, 4, 5, 7, 100_000):
                tail = f"{base}{sep}limit={limit}&offset={offset}"
                one = _atom_page(reader, f"/opds{tail}")
                two = _json_page(reader, f"{V2}{tail}")
                key = (who, tail)
                assert two["ids"] == one["ids"], key
                assert two["total"] == one["total"], key
                assert two["next"] == one["next"], key
                assert two["previous"] == one["previous"], key
                if who == "kid":
                    for hidden in HIDDEN_KID:
                        assert world.seed.works[hidden] not in json.dumps(two), key
                    if one["total"] is not None and base in ("/works", "/recent"):
                        assert one["total"] == len(SEEN_KID), key


@pytest.mark.parametrize("who", ["kid", "mum"])
def test_rev392_an_entry_of_a_work_has_the_same_notice_file_type_and_length(
    bundled: World, who: str
) -> None:
    """The fields the meta-test leaves out: the notice, the media type and the length of a file."""
    reader: Reader = getattr(bundled, who)
    keys = SEEN_KID if who == "kid" else set(bundled.seed.works)
    for key in sorted(keys):
        work = bundled.seed.works[key]
        one, two = reader.get(f"/opds/works/{work}"), reader.get(f"{V2}/works/{work}")
        assert one.status_code == two.status_code, (who, key)
        if one.status_code != 200:
            continue
        a, b = _atom_page(reader, f"/opds/works/{work}"), _json_page(reader, f"{V2}/works/{work}")
        assert sorted(b["entries"], key=str) == sorted(a["entries"], key=str), (who, key)


def test_rev392_hidden_absent_and_malformed_are_the_same_bodies_in_both_feeds(
    world: World,
) -> None:
    """Every byte of the problem, not only its shape: hidden, absent and malformed, 1.2 and 2.0.
    A 422 on a limit out of bounds is the same answer whatever the identifier."""
    kid = world.kid

    def body(path: str) -> tuple[int, dict[str, Any]]:
        got = kid.get(path)
        found = got.json()
        found.pop("request_id", None)
        found.pop("instance", None)
        return got.status_code, found

    ids = {
        "absent": ABSENT_ULID,
        "malformed": "not-a-ulid",
        **{key: world.seed.works[key] for key in HIDDEN_KID},
    }
    seen = {
        (feed, name): body(f"{feed}/works/{raw}")
        for feed in ("/opds", V2)
        for name, raw in ids.items()
    }
    assert len({json.dumps(v, sort_keys=True) for v in seen.values()}) == 1, seen
    over = {
        name: body(f"{feed}/series/{raw}?limit=51")
        for feed in ("/opds", V2)
        for name, raw in ids.items()
        if name != "malformed"
    }
    assert len({json.dumps(v, sort_keys=True) for v in over.values()}) == 1, over


# -- the client fallback under `/opds` (the finding of the author: `spa.py`) ----------------------

INDEX = (
    '<!doctype html><meta http-equiv="content-security-policy"'
    " content=\"default-src 'self'; script-src 'self' 'sha256-abc='\"><title>Shelfhold</title>"
)


@pytest.fixture
def spa_world(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> Iterator[World]:
    build = tmp_path / "build"
    build.mkdir()
    (build / "index.html").write_text(INDEX, encoding="utf-8")
    with make_world(monkeypatch, settings.model_copy(update={"frontend_dir": build})) as made:
        yield made


def test_rev392_an_unknown_opds_path_tells_nothing_of_what_is_hidden(spa_world: World) -> None:
    """An unknown path under `/opds` is a 404 problem (#394: no more `index.html`). It is no oracle
    as long as the answer is the same for a visible, a hidden and an absent identifier, with or
    without a token: these paths are decided by their shape, never by the catalogue."""
    w = spa_world
    anonymous = w.env.other_client("198.51.100.50")
    ids = {
        "visible": w.seed.works["ok"],
        "absent": ABSENT_ULID,
        **{key: w.seed.works[key] for key in HIDDEN_KID},
    }
    shapes = (
        "{feed}/works/{id}/",
        "{feed}/works/{id}/x",
        "{feed}/works/{id}/complete",
        "{feed}/series/{id}/x",
        "{feed}/shelves/{id}/x",
    )
    for feed in ("/opds", V2):
        for shape in shapes:
            answers = set()
            for raw in ids.values():
                path = shape.format(feed=feed, id=raw)
                for client in (w.kid, w.mum, anonymous):
                    got = client.get(path)
                    body = json.loads(got.text)
                    body.pop("request_id", None)  # the one thing that is made for each request
                    answers.add(
                        (got.status_code, got.headers.get("content-type"), json.dumps(body))
                    )
            assert len(answers) == 1, (feed, shape, answers)
            ((status, _type, text),) = answers
            assert status == 404 and text != INDEX, (
                feed,
                shape,
            )  # a problem, not the page of the SPA
            for raw in ids.values():
                assert raw not in text
            for key in HIDDEN_KID:
                assert w.seed.title_of(key) not in text
    # the feed itself still answers, and still decides by the catalogue (the control)
    assert w.kid.get(f"{V2}/works/{w.seed.works['ok']}").status_code == 200
    assert w.kid.get(f"{V2}/works/{w.seed.works['above']}").status_code == 404


def test_rev392_bundled_world_is_used(bundled: World) -> None:
    """The bench of the second probe offers a file for every visible Edition (the control)."""
    assert isinstance(bundled.bundles, FakeBundles)
    feed = json.loads(bundled.kid.get(f"{V2}/works/{bundled.seed.works['ok']}").content)
    assert any(
        x["rel"] == "http://opds-spec.org/acquisition"
        for p in feed["publications"]
        for x in p["links"]
    )
