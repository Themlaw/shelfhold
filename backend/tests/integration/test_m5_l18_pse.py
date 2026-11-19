"""M5 L18 (EF-INT-007, EF-BIB-015, EF-USR-006, ENF-SEC-010, ENF-SEC-013): OPDS-PSE page streaming.

An e-reader that speaks OPDS-PSE reads a volume or a chapter page by page, with its device token:
`pse:stream` is a link of the feed of a Work, and each page is `GET /opds/pse/{item}/{page}`
(`page` counted from 0, as the PSE specification does). The pages come from the kept file of an
exported volume, never an original, by the same filter as everything else:

- a child never sees a page of a Work she may not see, nor its existence: the same 404, the same
  shape, as for an item that does not exist; a feed names no hidden item;
- the profile is read again at every page: a Work reclassified between two pages stops at the next;
- the format is the client's to choose by `Accept` (the native one when it takes it, else a
  rendition of L20a), and nothing is made for a profile that may not see the page;
- reading writes nothing: no position, no history (PSE is read-only, ENF-SEC-013).

The attacks go through the real application: a child's token, a member's, no token, a wrong key,
a web session, an API key, an identifier that is another profile's (IDOR), malformed identifiers
and page numbers, an `Accept` that asks for a format nothing can make.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export, reading
from tests import page_formats_env as pf
from tests.content_env import PNG, FakeArchives, add_carrier
from tests.integration.test_l48_opds import (
    ABSENT_ULID,
    ATOM,
    World,
    auth_of,
    basic,
    crawl,
    make_world,
    parse,
)

pytestmark = pytest.mark.req("EF-INT-007")

PSE_NS = "{http://vaemendis.net/opds-pse/ns}"
STREAM = "http://vaemendis.net/opds-pse/stream"
needs_avif = pytest.mark.skipif(not pf.has_avif(), reason="this Pillow has no AVIF codec")


@dataclass
class Pse:
    world: World
    src: FakeArchives

    def stream(self, edition: str, pages: dict[str, bytes]) -> str:
        """An exported volume of the Edition whose kept file holds `pages`; its `export_item`."""
        engine = self.world.seed.engine
        content = add_carrier(engine, self.world.seed.editions[edition], format="cbz")
        item = new_ulid()
        with write_transaction(engine) as connection:
            connection.execute(
                sa.insert(export.export_item).values(
                    id=item, edition_id=self.world.seed.editions[edition], kind="unit",
                    content_id=content,
                )
            )  # fmt: skip
        self.src.by_content[content] = dict(pages)
        self.src.item_content[item] = content
        return item

    def page(self, who: Any, item: str, page: object, **headers: str) -> Any:
        return who.get(f"/opds/pse/{item}/{page}", headers=headers)


@pytest.fixture
def pse(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Pse]:
    src = FakeArchives()
    monkeypatch.setattr("shelfhold.main.readable_copies", lambda *_a, **_k: src)
    with make_world(monkeypatch, settings) as world:
        yield Pse(world, src)


def png_pages(count: int) -> dict[str, bytes]:
    return {f"p{n}.png": PNG + str(n).encode() for n in range(1, count + 1)}


def shape(response: Any) -> tuple[Any, ...]:
    """What a refusal says: status, code, length. A hidden thing and an absent one must agree."""
    body = response.json()
    return response.status_code, body.get("code"), len(response.content)


def stream_links(feed: ET.Element) -> list[dict[str, str]]:
    return [
        dict(node.attrib) for node in feed.iter(f"{ATOM}link") if node.attrib.get("rel") == STREAM
    ]


# -- the feed offers a stream, and only of what the profile may see ---------------------------


def test_opds_pse_link_is_a_stream_of_a_visible_volume(pse: Pse) -> None:
    item = pse.stream("ok", png_pages(3))
    feed = parse(pse.world.kid.get(f"/opds/works/{pse.world.seed.works['ok']}"))
    (found,) = stream_links(feed)
    assert found[f"{PSE_NS}count"] == "3"  # the number of pages, as the specification says
    assert found["type"] == "image/jpeg"
    assert f"/opds/pse/{item}/{{pageNumber}}" in found["href"]  # a template, from the root
    assert found["href"].startswith("/opds/")  # never a host, a scheme or a credential
    assert "@" not in found["href"] and "//" not in found["href"]


def test_opds_pse_stream_respects_visibility(pse: Pse) -> None:
    """The acceptance of EF-INT-007 for PSE: a child streams a page of what she may see, and a
    page of what she may not is an item that does not exist."""
    world = pse.world
    seen = pse.stream("ok", png_pages(2))
    hidden = {
        "above": pse.stream("above", png_pages(2)),  # 16 years: above her ceiling
        "twelve": pse.stream("twelve", png_pages(2)),  # 12 years
        "x": pse.stream("x", png_pages(2)),  # adult
        "misfiled": pse.stream("misfiled", png_pages(2)),  # unrated, on a shelf that is not hers
        "reserved": pse.stream("reserved", png_pages(2)),  # on a shelf reserved to another profile
    }
    assert pse.page(world.kid, seen, 0).status_code == 200
    absent = shape(pse.page(world.kid, ABSENT_ULID, 0))
    assert absent[0] == 404
    for key, item in hidden.items():
        got = pse.page(world.kid, item, 0)
        assert shape(got) == absent, key  # not a 403, not another body, not another length
        assert "etag" not in got.headers and "content-disposition" not in got.headers
        assert pse.page(world.mum, item, 0).status_code == 200, key  # the control: a member reads
    # the feed of a hidden Work is the feed of an absent one, and the feed of a visible one names
    # no hidden item
    for key in ("above", "twelve", "x"):
        gone = world.kid.get(f"/opds/works/{world.seed.works[key]}")
        assert shape(gone) == shape(world.kid.get(f"/opds/works/{ABSENT_ULID}")), key
    seen_feeds, _hrefs = crawl(world.kid)
    text = "".join(seen_feeds.values())
    for item in hidden.values():
        assert item not in text
    assert seen in "".join(
        crawl(world.kid, f"/opds/works/{world.seed.works['ok']}")[0].values()
    )  # the control: the visible volume is offered


def test_opds_feed_respects_visibility(pse: Pse) -> None:
    """The acceptance of EF-INT-007 extended to PSE: whatever a client browses, the streams it is
    offered are those of the content of its scope."""
    world = pse.world
    for key in ("ok", "adult_shelf", "mixed_rated", "above", "x", "twelve"):
        pse.stream(key, png_pages(2))
    offered = {
        key: stream_links(parse(world.kid.get(f"/opds/works/{work}")))
        if world.kid.get(f"/opds/works/{work}").status_code == 200
        else []
        for key, work in world.seed.works.items()
    }
    assert offered["ok"] and offered["adult_shelf"]
    for key in ("above", "x", "twelve", "flagged", "unrated"):
        assert offered[key] == [], key
    mum = parse(world.mum.get(f"/opds/works/{world.seed.works['above']}"))
    assert len(stream_links(mum)) == 1  # a member is not narrowed
    world.clean("".join(crawl(world.kid)[0].values()))


def test_opds_pse_hidden_edition_of_a_visible_work_is_not_offered(pse: Pse) -> None:
    """A Work with a visible Edition and a hidden one: the stream of the hidden one is neither
    listed nor served, and the page of the visible one is."""
    world = pse.world
    shown = pse.stream("mixed_rated", png_pages(2))
    hidden = pse.stream("mixed_new", png_pages(2))  # the Edition added later, not yet rated
    feed = parse(world.kid.get(f"/opds/works/{world.seed.works['mixed']}"))
    hrefs = " ".join(x["href"] for x in stream_links(feed))
    assert shown in hrefs and hidden not in hrefs
    assert pse.page(world.kid, shown, 0).status_code == 200
    assert shape(pse.page(world.kid, hidden, 0)) == shape(pse.page(world.kid, ABSENT_ULID, 0))


# -- a page, once streamed, is read again at the next ------------------------------------------


def test_opds_pse_profile_is_checked_again_at_every_page(pse: Pse) -> None:
    world = pse.world
    item = pse.stream("ok", png_pages(3))
    assert pse.page(world.kid, item, 0).status_code == 200
    with world.seed.engine.begin() as connection:  # the Work is reclassified, between two pages
        connection.execute(
            sa.update(catalog.work)
            .where(catalog.work.c.id == world.seed.works["ok"])
            .values(age_rating=16)
        )
    after = pse.page(world.kid, item, 1)
    assert shape(after) == shape(pse.page(world.kid, ABSENT_ULID, 1))
    assert "etag" not in after.headers
    assert pse.page(world.mum, item, 1).status_code == 200


def test_opds_pse_revoked_token_stops_the_next_page(pse: Pse) -> None:
    world = pse.world
    item = pse.stream("ok", png_pages(2))
    assert pse.page(world.kid, item, 0).status_code == 200
    assert auth_of(world.env).revoke_device_token(world.kid.public_id)
    assert pse.page(world.kid, item, 1).status_code == 401


# -- pages ----------------------------------------------------------------------------------


def test_opds_pse_page_is_the_page_of_the_file_in_reading_order(pse: Pse) -> None:
    pages = {f"p{n}.png": PNG + str(n).encode() for n in (1, 2, 10, 3)}
    item = pse.stream("ok", pages)
    bodies = [pse.page(pse.world.kid, item, n) for n in range(4)]
    assert [b.status_code for b in bodies] == [200] * 4
    assert [b.content for b in bodies] == [pages[f"p{n}.png"] for n in (1, 2, 3, 10)]  # p2 < p10
    first = bodies[0]
    assert first.headers["content-type"] == "image/png"
    assert first.headers["content-security-policy"].startswith("sandbox")
    assert first.headers["x-content-type-options"] == "nosniff"
    assert first.headers["cache-control"].startswith("private")
    assert "content-disposition" not in first.headers  # a raster image is shown, not downloaded
    assert "authorization" in first.headers["vary"].lower()


@pytest.mark.parametrize(
    "page", ["3", "4", "-1", "abc", "1.5", "%2e%2e", "0x1", "99999999999999999999", "%201", "٣"]
)
def test_opds_pse_a_page_that_is_not_one_is_a_404(pse: Pse, page: str) -> None:
    """Past the last page, negative, not a number, not an ASCII digit: the answer of an absent
    thing (the pages are `0` to `2`)."""
    item = pse.stream("ok", png_pages(3))
    got = pse.world.kid.get(f"/opds/pse/{item}/{page}")
    assert got.status_code == 404, page
    assert shape(got) == shape(pse.page(pse.world.kid, ABSENT_ULID, 0))


@pytest.mark.parametrize(
    "item", ["not-an-id", "01HZZZZZZZZZZZZZZZZZZZZZZ", "%00", "01HZZZZZZZZZZZZZZZZZZZZZZMX"]
)
def test_opds_pse_an_identifier_that_is_not_one_is_the_answer_of_an_absent_one(
    pse: Pse, item: str
) -> None:
    got = pse.world.kid.get(f"/opds/pse/{item}/0")
    assert got.status_code == 404
    assert shape(got) == shape(pse.page(pse.world.kid, ABSENT_ULID, 0))


@pytest.mark.parametrize("path", ["../etc/0", "..%2f..%2fapi%2fsession/0", "%2e%2e/%2e%2e/0"])
def test_opds_pse_a_path_that_climbs_serves_nothing(pse: Pse, path: str) -> None:
    got = pse.world.kid.get(f"/opds/pse/{path}")
    assert got.status_code in {404, 405}
    assert "etag" not in got.headers and not got.content.startswith(b"\x89PNG")


def test_opds_pse_a_volume_without_a_readable_copy_is_a_404(pse: Pse) -> None:
    item = pse.stream("ok", png_pages(2))
    pse.src.by_content.clear()  # the kept file is gone: nothing is made up, nothing is said
    assert shape(pse.page(pse.world.kid, item, 0)) == shape(pse.page(pse.world.kid, ABSENT_ULID, 0))
    feed = parse(pse.world.kid.get(f"/opds/works/{pse.world.seed.works['ok']}"))
    assert stream_links(feed) == []  # and no stream is offered for it


def test_opds_pse_a_content_that_is_not_a_cbz_is_not_streamed(pse: Pse) -> None:
    world = pse.world
    content = add_carrier(world.seed.engine, world.seed.editions["ok"], format="epub")
    item = new_ulid()
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.insert(export.export_item).values(
                id=item, edition_id=world.seed.editions["ok"], kind="unit", content_id=content
            )
        )
    pse.src.by_content[content] = png_pages(2)
    pse.src.item_content[item] = content
    assert pse.page(world.kid, item, 0).status_code == 404


def test_opds_pse_an_item_whose_content_is_hidden_serves_nothing(pse: Pse) -> None:
    """An `export_item` names an Edition and a content, and nothing ties the two: a visible
    Edition's item that points at the content of a hidden one is not a way to read it."""
    world = pse.world
    hidden_content = add_carrier(world.seed.engine, world.seed.editions["above"], format="cbz")
    item = pse.stream("ok", png_pages(2))
    pse.src.by_content[hidden_content] = png_pages(2)
    with world.seed.engine.begin() as connection:
        connection.execute(
            sa.update(export.export_item)
            .where(export.export_item.c.id == item)
            .values(content_id=hidden_content)
        )
    pse.src.item_content[item] = hidden_content
    assert shape(pse.page(world.kid, item, 0)) == shape(pse.page(world.kid, ABSENT_ULID, 0))


# -- the credential is the device token, and nothing else --------------------------------------


def test_opds_pse_needs_the_device_token(pse: Pse) -> None:
    world = pse.world
    item = pse.stream("ok", png_pages(2))
    path = f"/opds/pse/{item}/0"
    anonymous = world.env.other_client("198.51.100.20")
    assert anonymous.get(path).status_code == 401
    assert anonymous.get(path, headers=basic("nobody", "x" * 20)).status_code == 401
    assert world.kid_session.get(path).status_code == 401  # a web session is not a device
    key = auth_of(world.env).create_api_key(name="k", username="kid", read_only=True).value
    other = world.env.other_client("198.51.100.21")
    assert other.get(path, headers={"Authorization": f"Bearer {key}"}).status_code == 401
    assert other.get(path, headers=basic("kid", key)).status_code == 401
    assert world.kid.get(path).status_code == 200  # the control


def test_opds_pse_a_token_of_a_profile_is_that_profiles_alone(pse: Pse) -> None:
    """IDOR: the identifier is no capability. Another profile's token reads what that profile may
    see, and the child's token reads nothing of a member's reserved shelf."""
    world = pse.world
    item = pse.stream("reserved", png_pages(1))
    assert world.mum.get(f"/opds/pse/{item}/0").status_code == 200
    assert shape(pse.page(world.kid, item, 0)) == shape(pse.page(world.kid, ABSENT_ULID, 0))


def test_opds_pse_is_not_reachable_with_a_token_under_content_or_api(pse: Pse) -> None:
    """The token opens `/opds` and `/kosync`; the page of a book is not on any other road."""
    item = pse.stream("ok", png_pages(1))
    for path in (f"/content/x/{item}", f"/api/reader/export_item/{item}/publication"):
        assert pse.world.kid.get(path).status_code in {401, 403, 404}


# -- the format is the client's to choose ------------------------------------------------------


def webp_pages() -> dict[str, bytes]:
    return {"p1.webp": pf.webp(), "p2.webp": pf.webp((200, 30, 30))}


def test_opds_pse_page_format_negotiated_by_accept(pse: Pse) -> None:
    """EF-BIB-015: the native page when the client takes it, else a rendition; one the client
    cannot take and nothing can make is a 406, after the profile has been checked."""
    world = pse.world
    item = pse.stream("ok", webp_pages())

    def kind(response: Any) -> str:
        return str(response.headers["content-type"]).split(";")[0]

    native = pse.page(world.kid, item, 0)  # no Accept: the page as it is
    assert (native.status_code, kind(native), native.content) == (200, "image/webp", pf.webp())
    for accept in ("image/webp", "image/*", "*/*", "image/webp,image/jpeg;q=0.5", "image/avif,*/*"):
        got = pse.page(world.kid, item, 0, Accept=accept)
        assert (got.status_code, kind(got), got.content) == (200, "image/webp", pf.webp()), accept
    jpeg = pse.page(world.kid, item, 0, Accept="image/jpeg")
    assert (jpeg.status_code, kind(jpeg)) == (200, "image/jpeg")
    assert jpeg.content[:3] == b"\xff\xd8\xff" and jpeg.content != pf.webp()
    assert (
        "authorization" in jpeg.headers["vary"].lower() and "accept" in jpeg.headers["vary"].lower()
    )
    png = pse.page(world.kid, item, 1, Accept="image/png")
    assert (png.status_code, kind(png)) == (200, "image/png")
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n"
    # the order of preference is the client's: q-values, and an explicit exclusion
    both = pse.page(world.kid, item, 0, Accept="image/jpeg;q=0.4, image/png;q=0.9")
    assert kind(both) == "image/png"
    excluded = pse.page(world.kid, item, 0, Accept="image/webp;q=0, image/jpeg")
    assert kind(excluded) == "image/jpeg"
    wildcard_out = pse.page(world.kid, item, 0, Accept="image/*, image/webp;q=0")
    assert kind(wildcard_out) == "image/jpeg"
    # nothing the client takes: not acceptable, and said the same however it was asked
    for accept in ("text/html", "image/gif", "application/json", "image/avif"):
        refused = pse.page(world.kid, item, 0, Accept=accept)
        assert refused.status_code == 406, accept
        assert refused.json()["code"] == "not_acceptable"
    assert pse.page(world.kid, item, 0, Accept=";;;, q=").status_code in {200, 406}


def test_opds_pse_a_page_that_is_native_to_the_client_is_never_converted(pse: Pse) -> None:
    item = pse.stream("ok", {"p1.png": PNG + b"1"})
    assert pse.page(pse.world.kid, item, 0, Accept="image/png").content == PNG + b"1"
    assert pse.page(pse.world.kid, item, 0, Accept="image/jpeg").status_code == 406  # not a source
    with pse.world.seed.engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM rendition")).scalar_one() == 0


def test_opds_pse_a_rendition_is_made_once_and_for_a_profile_who_may_see(pse: Pse) -> None:
    world = pse.world
    item = pse.stream("ok", webp_pages())
    hidden = pse.stream("above", webp_pages())
    first = pse.page(world.kid, item, 0, Accept="image/jpeg")
    second = pse.page(world.kid, item, 0, Accept="image/jpeg")
    assert first.content == second.content and first.status_code == 200
    with world.seed.engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM rendition")).scalar_one() == 1
    # nothing is converted for a page the profile may not see, and an unacceptable format is no
    # oracle on it: the hidden page answers 404, whatever is asked
    for accept in ("image/jpeg", "text/html", "image/png"):
        assert shape(pse.page(world.kid, hidden, 0, Accept=accept)) == shape(
            pse.page(world.kid, ABSENT_ULID, 0, Accept=accept)
        ), accept
    with world.seed.engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM rendition")).scalar_one() == 1


@needs_avif
def test_opds_pse_an_avif_page_is_converted_for_a_client_that_lacks_it(pse: Pse) -> None:
    item = pse.stream("ok", {"p1.avif": pf.avif()})
    native = pse.page(pse.world.kid, item, 0, Accept="image/avif")
    assert native.headers["content-type"].startswith("image/avif")
    converted = pse.page(pse.world.kid, item, 0, Accept="image/jpeg, image/png;q=0.5")
    assert converted.headers["content-type"].startswith("image/jpeg")
    assert converted.content[:3] == b"\xff\xd8\xff"


def test_opds_pse_a_page_that_cannot_be_decoded_is_refused_not_served_raw(pse: Pse) -> None:
    item = pse.stream("ok", {"p1.webp": b"RIFF\x04\x00\x00\x00WEBP"})  # a header and nothing else
    got = pse.page(pse.world.kid, item, 0, Accept="image/jpeg")
    assert got.status_code == 406
    assert got.content[:4] != b"RIFF"


# -- reading writes nothing (ENF-SEC-013) -------------------------------------------------------


def snapshot(engine: sa.Engine) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of the reading tables and of the catalogue: what a read-only door leaves alone."""
    out: dict[str, list[tuple[Any, ...]]] = {}
    with engine.connect() as connection:
        for table in (
            reading.reading_position,
            reading.progress_sync_cursor,
            reading.progress_sync_note,
            reading.read_state_push_note,
            catalog.work,
            catalog.unit,
            export.export_item,
        ):
            rows = connection.execute(sa.select(table)).all()
            out[table.name] = sorted(json.dumps(list(r), default=str) for r in rows)
    return out


def test_opds_pse_streaming_writes_no_position(pse: Pse) -> None:
    world = pse.world
    item = pse.stream("ok", {**webp_pages(), "p3.png": PNG})
    before = snapshot(world.seed.engine)
    feed = parse(world.kid.get(f"/opds/works/{world.seed.works['ok']}"))
    assert stream_links(feed)
    for n in range(3):
        assert pse.page(world.kid, item, n, Accept="image/jpeg, image/png").status_code == 200
    assert pse.page(world.kid, item, 7).status_code == 404
    assert snapshot(world.seed.engine) == before  # no position, no history, no mark
    with world.seed.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(reading.reading_position)
            ).scalar_one()
            == 0
        )


def test_opds_pse_link_carries_no_last_read_state(pse: Pse) -> None:
    """The position a client may keep is kosync's; the feed claims no `lastRead` of its own."""
    pse.stream("ok", png_pages(2))
    text = pse.world.kid.get(f"/opds/works/{pse.world.seed.works['ok']}").text
    assert "lastRead" not in text and "lastReadDate" not in text
