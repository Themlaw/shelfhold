"""M5 L20b (EF-BIB-015, EF-INT-007, EF-LEC-010, EF-USR-006, ENF-SEC-010): a KEPUB for a Kobo,
offered in the feed of a Work (`/opds`) and on the page of a Work (`/lite`), served from the cache
of L20a.

What is offered is the exported volume of a Work the profile may see, whose kept file is an EPUB,
and that the profile does not read elsewhere (ADR-0055 D-P1, as for OPDS-PSE). The KEPUB is made
from the neutralized derivative, never an original, after the profile has been judged on the Edition
and on the content, at every request. A KEPUB of a hidden Work is the answer for an absent one, and
the cache tells one profile nothing of what another read.

The attacks go through the real application: a child's token and a member's, no token, a wrong
key, a web session, an API key, an identifier that is another profile's (IDOR) or malformed, a
ceiling lowered under a feed, a book that cannot be converted, and a profile that holds every slot.
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from tests.content_env import FakeArchive, FakeArchives, add_carrier, user_id_of
from tests.integration.test_l48_opds import (
    ABSENT_ULID,
    ACQUISITION,
    World,
    auth_of,
    basic,
    crawl,
    entries,
    link,
    make_world,
    parse,
)
from tests.integration.test_m5_l17a_lite import Links, signed_in
from tests.unit.test_kepub import book, epub

pytestmark = pytest.mark.req("EF-BIB-015")

KEPUB = "application/kepub+zip"


class BookArchive(FakeArchive):
    def __init__(self, raw: bytes, entries: dict[str, bytes], asked: list[str], pin: str) -> None:
        super().__init__(entries, asked, pin)
        self._raw = raw

    def whole(self, max_bytes: int) -> bytes:
        assert len(self._raw) <= max_bytes
        return self._raw


class Shelf(FakeArchives):
    """The port of the archives of a carrier, with the file itself for a full read."""

    def __init__(self) -> None:
        super().__init__()
        self.raw: dict[str, bytes] = {}

    def open(self, carrier_type: str, carrier_id: str, scope: str = "full") -> Any:
        archive = super().open(carrier_type, carrier_id, scope)
        content = carrier_id if carrier_type == "content" else self.item_content[carrier_id]
        raw = self.raw.get(content, b"")
        return BookArchive(raw, dict(self.by_content[content]), self.asked, archive._pin)


@dataclass
class Kobo:
    world: World
    src: Shelf

    def volume(
        self,
        edition: str,
        *,
        live: bool = True,
        read_elsewhere: tuple[str, ...] = (),
        fmt: str = "epub",
        data: bytes | None = None,
    ) -> str:
        """An exported volume of the Edition whose kept file is `data` (an EPUB of the default
        synthetic book); its `export_item`."""
        engine = self.world.seed.engine
        content = add_carrier(engine, self.world.seed.editions[edition], format=fmt)
        item = new_ulid()
        with write_transaction(engine) as connection:
            connection.execute(
                sa.insert(export.export_item).values(
                    id=item, edition_id=self.world.seed.editions[edition], kind="unit",
                    content_id=content,
                )
            )  # fmt: skip
        if live:
            self._export(item, read_elsewhere)
        raw = epub(book()) if data is None else data
        self.src.raw[content] = raw
        self.src.by_content[content] = {"mimetype": b"application/epub+zip"}
        self.src.item_content[item] = content
        return item

    def _export(self, item: str, readers: tuple[str, ...]) -> None:
        from shelfhold.infrastructure.schema import identity
        from tests.integration.test_schema_storage_export import add_destination, add_target

        with write_transaction(self.world.seed.engine) as connection:
            destination = add_destination(
                connection, self.world.seed.shelves["adult"], add_target(connection)
            )
            connection.execute(
                sa.text("INSERT OR IGNORE INTO secret_key (kid, state) VALUES ('k1', 'active')")
            )
            for name in readers:
                user = connection.execute(
                    sa.select(identity.user.c.id).where(identity.user.c.username == name)
                ).scalar_one()
                connection.execute(
                    sa.insert(identity.destination_account).values(
                        id=new_ulid(), user_id=user, shelf_destination_id=destination,
                        remote_user=f"remote-{name}", secret_v=1, secret_kid="k1",  # noqa: S106
                        secret_nonce=os.urandom(12), secret_ct=os.urandom(32),
                        token_fingerprint="f" * 12, verified_at=sa.func.current_timestamp(),
                    )
                )  # fmt: skip
            connection.execute(
                sa.insert(export.export_record).values(
                    shelf_destination_id=destination,
                    export_item_id=item,
                    locator_type="none",
                    state="written",
                )
            )

    def get(self, who: Any, item: str, **headers: str) -> Any:
        return who.get(f"/opds/kepub/{item}", headers=headers)

    def rows(self) -> int:
        with self.world.seed.engine.connect() as connection:
            return int(connection.execute(sa.text("SELECT count(*) FROM rendition")).scalar_one())


@pytest.fixture
def kobo(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Kobo]:
    src = Shelf()
    monkeypatch.setattr("shelfhold.main.readable_copies", lambda *_a, **_k: src)
    with make_world(monkeypatch, settings) as world:
        yield Kobo(world, src)


def shape(response: Any) -> tuple[Any, ...]:
    """What a refusal says: status, code, length. A hidden thing and an absent one must agree."""
    return response.status_code, response.json().get("code"), len(response.content)


def kepub_links(feed: ET.Element) -> list[dict[str, str]]:
    return [
        dict(node.attrib)
        for node in feed.iter("{http://www.w3.org/2005/Atom}link")
        if node.attrib.get("rel") == ACQUISITION and node.attrib.get("type") == KEPUB
    ]


# -- the feed offers a KEPUB, and only of what the profile may see ------------------------------


def test_opds_kepub_is_offered_for_a_visible_volume_and_served(kobo: Kobo) -> None:
    item = kobo.volume("ok")
    feed = parse(kobo.world.kid.get(f"/opds/works/{kobo.world.seed.works['ok']}"))
    (found,) = kepub_links(feed)
    assert found["href"] == f"/opds/kepub/{item}"  # from the root: no host, scheme or credential
    got = kobo.get(kobo.world.kid, item)
    assert got.status_code == 200 and got.headers["content-type"] == KEPUB
    assert got.headers["content-disposition"].startswith("attachment;")
    assert ".kepub.epub" in got.headers["content-disposition"]
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["content-security-policy"].startswith("sandbox")
    assert "private" in got.headers["cache-control"] and "etag" not in got.headers
    assert got.headers["content-length"] == str(len(got.content))
    with zipfile.ZipFile(io.BytesIO(got.content)) as archive:
        assert archive.namelist()[0] == "mimetype"  # the book is intact: spine, toc, spans
        assert b"koboSpan" in archive.read("OEBPS/chapter1.xhtml")
        assert archive.read("OEBPS/toc.ncx") == book()["OEBPS/toc.ncx"]


def test_opds_kepub_respects_visibility(kobo: Kobo) -> None:
    """A child gets the KEPUB of what she may see; the KEPUB of what she may not is an item that
    does not exist, and no feed she can read names it."""
    world = kobo.world
    seen = kobo.volume("ok")
    hidden = {
        "above": kobo.volume("above"),
        "twelve": kobo.volume("twelve"),
        "x": kobo.volume("x"),
        "misfiled": kobo.volume("misfiled"),
        "reserved": kobo.volume("reserved"),
    }
    assert kobo.get(world.kid, seen).status_code == 200
    absent = shape(kobo.get(world.kid, ABSENT_ULID))
    assert absent[0] == 404
    for key, item in hidden.items():
        got = kobo.get(world.kid, item)
        assert shape(got) == absent, key  # not a 403, not another body, not another length
        assert "content-disposition" not in got.headers and "etag" not in got.headers
        assert kobo.get(world.mum, item).status_code == 200, key  # the control: a member reads
    seen_feeds, _ = crawl(world.kid)
    assert not [i for i in hidden.values() if i in "".join(seen_feeds.values())]
    feed = "".join(crawl(world.kid, f"/opds/works/{world.seed.works['ok']}")[0].values())
    assert seen in feed  # the control: the visible volume is offered


def test_opds_kepub_the_cache_tells_a_child_nothing_of_what_a_member_read(kobo: Kobo) -> None:
    """The member makes the KEPUB of a Work above the ceiling; the child asking for it gets the
    answer of an absent one, with no read of the cache, and the row stays the member's business."""
    world = kobo.world
    item = kobo.volume("above")
    assert kobo.get(world.mum, item).status_code == 200 and kobo.rows() == 1
    asked = len(kobo.src.asked)
    assert shape(kobo.get(world.kid, item)) == shape(kobo.get(world.kid, ABSENT_ULID))
    assert len(kobo.src.asked) == asked and kobo.rows() == 1


def test_opds_kepub_is_checked_again_at_every_request(kobo: Kobo) -> None:
    item = kobo.volume("ok_second")
    edition = kobo.world.seed.editions["ok_second"]
    assert kobo.get(kobo.world.kid, item).status_code == 200
    with kobo.world.seed.engine.begin() as conn:  # re-rated above her ceiling since the feed
        conn.execute(
            sa.update(catalog.edition).where(catalog.edition.c.id == edition).values(age_rating=18)
        )
    assert shape(kobo.get(kobo.world.kid, item)) == shape(kobo.get(kobo.world.kid, ABSENT_ULID))
    assert kobo.get(kobo.world.mum, item).status_code == 200


def test_opds_kepub_a_revoked_token_stops_the_next_request(kobo: Kobo) -> None:
    item = kobo.volume("ok")
    assert kobo.get(kobo.world.kid, item).status_code == 200
    auth = auth_of(kobo.world.env)
    created = auth.create_device_token(subject_username="kid", name="salon")
    client = kobo.world.env.other_client("198.51.100.9")
    headers = basic(created.public_id, created.secret)
    assert client.get(f"/opds/kepub/{item}", headers=headers).status_code == 200
    assert auth.revoke_device_token(created.public_id) is True
    assert client.get(f"/opds/kepub/{item}", headers=headers).status_code == 401


# -- what is not offered -----------------------------------------------------------------------


def test_opds_kepub_an_item_never_exported_is_not_offered_nor_served(kobo: Kobo) -> None:
    item = kobo.volume("ok", live=False)
    feed = parse(kobo.world.mum.get(f"/opds/works/{kobo.world.seed.works['ok']}"))
    assert kepub_links(feed) == []
    assert kobo.get(kobo.world.mum, item).status_code == 404 and kobo.rows() == 0


def test_opds_kepub_what_the_profile_reads_elsewhere_is_not_a_second_full_copy(
    kobo: Kobo,
) -> None:
    """D-P1: a profile with an account of her own on the destination that holds the file reads it
    there; the KEPUB is a full copy out, so it is not offered to her, and is to another."""
    item = kobo.volume("ok", read_elsewhere=("mum",))
    work = f"/opds/works/{kobo.world.seed.works['ok']}"
    assert kepub_links(parse(kobo.world.mum.get(work))) == []
    assert kobo.get(kobo.world.mum, item).status_code == 404
    assert len(kepub_links(parse(kobo.world.kid.get(work)))) == 1
    assert kobo.get(kobo.world.kid, item).status_code == 200


def test_opds_kepub_a_content_that_is_not_an_epub_is_not_converted(kobo: Kobo) -> None:
    item = kobo.volume("ok", fmt="cbz")
    work = f"/opds/works/{kobo.world.seed.works['ok']}"
    assert kepub_links(parse(kobo.world.mum.get(work))) == []
    assert kobo.get(kobo.world.mum, item).status_code == 404 and kobo.rows() == 0


def test_opds_kepub_a_book_that_cannot_be_converted_is_a_404_and_leaves_nothing(
    kobo: Kobo,
) -> None:
    item = kobo.volume("ok", data=b"PK\x03\x04 not a book")
    got = kobo.get(kobo.world.mum, item)
    assert shape(got) == shape(kobo.get(kobo.world.mum, ABSENT_ULID))
    assert kobo.rows() == 0
    cache = kobo.world.env.settings.data_dir / "cache" / "renditions"
    assert not cache.exists() or not [p for p in cache.rglob("*") if p.is_file()]


def test_opds_kepub_a_volume_without_a_readable_copy_is_a_404(kobo: Kobo) -> None:
    item = kobo.volume("ok")
    kobo.src.by_content.pop(kobo.src.item_content[item])
    assert kobo.get(kobo.world.mum, item).status_code == 404


# -- the credential is the device token, and nothing else --------------------------------------


def test_opds_kepub_needs_the_device_token(kobo: Kobo) -> None:
    world = kobo.world
    item = kobo.volume("ok")
    path = f"/opds/kepub/{item}"
    anonymous = world.env.other_client("198.51.100.30")
    assert anonymous.get(path).status_code == 401
    assert anonymous.get(path, headers=basic("nobody", "x" * 20)).status_code == 401
    assert world.kid_session.get(path).status_code == 401  # a web session is not a device
    key = auth_of(world.env).create_api_key(name="k", username="kid", read_only=True).value
    other = world.env.other_client("198.51.100.31")
    assert other.get(path, headers={"Authorization": f"Bearer {key}"}).status_code == 401
    assert other.get(path, headers=basic("kid", key)).status_code == 401
    assert world.kid.get(path).status_code == 200  # the control


def test_opds_kepub_a_token_of_a_profile_is_that_profiles_alone(kobo: Kobo) -> None:
    """IDOR: the identifier is no capability. Another profile's token reads what that profile may
    see, and the child's token reads nothing of a member's reserved shelf."""
    item = kobo.volume("reserved")
    assert kobo.get(kobo.world.mum, item).status_code == 200
    assert shape(kobo.get(kobo.world.kid, item)) == shape(kobo.get(kobo.world.kid, ABSENT_ULID))


@pytest.mark.parametrize(
    "ident",
    ["..%2f..%2fetc%2fpasswd", "%2e%2e", "01hzzzzzzzzzzzzzzzzzzzzzzm", "a" * 5000, "..;", "%00"],
)
def test_opds_kepub_an_identifier_that_is_not_one_is_the_answer_of_an_absent_one(
    kobo: Kobo, ident: str
) -> None:
    got = kobo.world.mum.get(f"/opds/kepub/{ident}")
    assert got.status_code == 404 and "content-disposition" not in got.headers
    assert kobo.rows() == 0 and kobo.src.asked == []  # nothing was looked up, nothing made


def test_opds_kepub_is_not_reachable_with_a_token_under_content_or_api(kobo: Kobo) -> None:
    item = kobo.volume("ok")
    for path in (f"/content/x/{item}", f"/api/kepub/{item}", f"/kepub/{item}"):
        assert kobo.world.kid.get(path).status_code in {401, 403, 404}


def test_opds_kepub_is_read_only(kobo: Kobo) -> None:
    """Serving a book writes no position, no history, no mark (ENF-SEC-013)."""
    from shelfhold.infrastructure.schema import reading

    item = kobo.volume("ok")
    assert kobo.get(kobo.world.kid, item).status_code == 200
    with kobo.world.seed.engine.connect() as connection:
        count = sa.select(sa.func.count()).select_from(reading.reading_position)
        assert connection.execute(count).scalar_one() == 0


# -- the cache and the bound on threads --------------------------------------------------------


def test_opds_kepub_is_made_once_and_served_to_every_profile_who_may_see(kobo: Kobo) -> None:
    item = kobo.volume("ok")
    first = kobo.get(kobo.world.kid, item)
    second = kobo.get(kobo.world.mum, item)
    assert first.content == second.content and kobo.rows() == 1


def test_opds_kepub_a_profile_that_holds_every_slot_cannot_tell_what_another_read(
    kobo: Kobo,
) -> None:
    """C1 of the review of OPDS-PSE: the refusal for lack of a thread is decided before the cache
    is looked in, so a profile at its limit gets the same answer for a book another profile made
    and for one nobody made."""
    world = kobo.world
    made = kobo.volume("ok")
    fresh = kobo.volume("ok_second")
    assert kobo.get(world.mum, made).status_code == 200  # mum read it: it is in the cache
    renditions = world.env.app.state.renditions  # type: ignore[attr-defined]
    kid = user_id_of(world.seed.engine, "kid")
    with renditions.slot(kid), renditions.slot(kid):  # the child holds her own two
        cached, uncached = kobo.get(world.kid, made), kobo.get(world.kid, fresh)
        assert cached.status_code == uncached.status_code == 429
        assert shape(cached) == shape(uncached)
        assert int(cached.headers["retry-after"]) >= 1
        assert kobo.get(world.mum, fresh).status_code == 200  # the bound is per profile
    assert kobo.get(world.kid, made).status_code == 200  # given back


def test_opds_kepub_a_busy_answer_comes_after_the_visibility_not_before(kobo: Kobo) -> None:
    world = kobo.world
    hidden = kobo.volume("above")
    renditions = world.env.app.state.renditions  # type: ignore[attr-defined]
    kid = user_id_of(world.seed.engine, "kid")
    with renditions.slot(kid), renditions.slot(kid):
        assert shape(kobo.get(world.kid, hidden)) == shape(kobo.get(world.kid, ABSENT_ULID))


# -- /lite ------------------------------------------------------------------------------------


def test_lite_kepub_is_a_link_of_the_page_of_a_work_and_is_served(kobo: Kobo) -> None:
    item = kobo.volume("ok")
    client = signed_in(kobo.world, kobo.world.kid, "10.0.1.40")
    page = client.get(f"/lite/works/{kobo.world.seed.works['ok']}")
    assert page.status_code == 200
    hrefs = [h for h in Links(page.text).hrefs if "/kepub/" in h]
    assert hrefs == [f"/lite/kepub/{item}"]
    got = client.get(hrefs[0])
    assert got.status_code == 200 and got.headers["content-type"] == KEPUB
    assert got.headers["content-disposition"].startswith("attachment;")
    assert ".kepub.epub" in got.headers["content-disposition"]
    assert got.headers["content-security-policy"].startswith("sandbox")
    assert got.headers["cache-control"] == "no-store"  # the cookie is the credential
    with zipfile.ZipFile(io.BytesIO(got.content)) as archive:
        assert b"koboSpan" in archive.read("OEBPS/chapter1.xhtml")


def test_lite_kepub_respects_visibility_and_is_the_absent_page(kobo: Kobo) -> None:
    world = kobo.world
    hidden = kobo.volume("above")
    seen = kobo.volume("ok")
    client = signed_in(world, world.kid, "10.0.1.41")
    absent = client.get(f"/lite/kepub/{ABSENT_ULID}")
    got = client.get(f"/lite/kepub/{hidden}")
    assert absent.status_code == got.status_code == 404
    assert got.text == absent.text and "content-disposition" not in got.headers
    assert client.get(f"/lite/kepub/{seen}").status_code == 200
    page = client.get(f"/lite/works/{world.seed.works['above']}")
    assert page.status_code == 404 and hidden not in page.text
    member = signed_in(world, world.mum, "10.0.1.42")
    assert member.get(f"/lite/kepub/{hidden}").status_code == 200  # the control


def test_lite_kepub_needs_the_cookie_of_the_device_sign_in(kobo: Kobo) -> None:
    item = kobo.volume("ok")
    anonymous = kobo.world.env.other_client("198.51.100.32", "https://testserver")
    got = anonymous.get(f"/lite/kepub/{item}", follow_redirects=False)
    assert got.status_code in {303, 401, 404} and "koboSpan" not in got.text
    web = kobo.world.kid_session.get(f"/lite/kepub/{item}", follow_redirects=False)
    assert web.status_code in {303, 401, 404} and "koboSpan" not in web.text


def test_lite_kepub_a_profile_that_holds_every_slot_gets_a_page_to_come_back(kobo: Kobo) -> None:
    world = kobo.world
    item = kobo.volume("ok")
    client = signed_in(world, world.kid, "10.0.1.43")
    renditions = world.env.app.state.renditions  # type: ignore[attr-defined]
    kid = user_id_of(world.seed.engine, "kid")
    with renditions.slot(kid), renditions.slot(kid):
        got = client.get(f"/lite/kepub/{item}")
    assert got.status_code == 429 and int(got.headers["retry-after"]) >= 1
    assert "text/html" in got.headers["content-type"]


def test_the_work_entry_of_a_feed_holds_a_kepub_acquisition_with_its_title(kobo: Kobo) -> None:
    kobo.volume("ok")
    feed = parse(kobo.world.kid.get(f"/opds/works/{kobo.world.seed.works['ok']}"))
    found = [e for e in entries(feed) if (link(e, ACQUISITION) or {}).get("type") == KEPUB]
    assert len(found) == 1
    text = ET.tostring(found[0], encoding="unicode")
    assert re.search(r"<(\w+:)?title>[^<]+</", text)


def test_opds_kepub_the_profile_is_judged_again_when_the_book_is_made(kobo: Kobo) -> None:
    """Review of #395: an account that appears on the destination while the conversion runs makes
    the volume one she reads elsewhere: the answer is the 404 of an absent one, not the book."""
    item = kobo.volume("ok")
    renditions = kobo.world.env.app.state.renditions  # type: ignore[attr-defined]
    real = renditions._kepub

    def meanwhile(data: bytes) -> bytes:
        kobo._export(item, ("mum",))  # she links her own account to a destination that holds it
        return real(data)  # type: ignore[no-any-return]

    renditions._kepub = meanwhile
    got = kobo.get(kobo.world.mum, item)
    assert shape(got) == shape(kobo.get(kobo.world.mum, ABSENT_ULID))
    assert "content-disposition" not in got.headers


def test_opds_kepub_a_disk_that_cannot_be_written_is_an_error_not_an_absent_book(
    kobo: Kobo,
) -> None:
    """Review of #395: only what the profile may not see, or cannot be read, is a 404; a failure of
    the cache itself is the server's."""
    item = kobo.volume("ok")
    renditions = kobo.world.env.app.state.renditions  # type: ignore[attr-defined]

    def full(data: bytes) -> bytes:
        raise OSError("no space left")

    renditions._kepub = full
    with pytest.raises(OSError, match="no space"):
        kobo.get(kobo.world.mum, item)


# -- OPDS 2.0 (L19): the same offer, in the same entries ---------------------------------------


def v2_kepubs(feed: Any) -> list[dict[str, Any]]:
    from tests.integration.test_m5_l19_opds2 import publications

    return [
        x
        for p in publications(feed)
        for x in p["links"]
        if x["rel"] == ACQUISITION and x["type"] == KEPUB
    ]


def test_opds2_kepub_is_offered_like_in_opds1_and_judged_the_same(kobo: Kobo) -> None:
    from tests.integration.test_m5_l19_opds2 import V2, doc

    world = kobo.world
    seen = kobo.volume("ok")
    elsewhere = kobo.volume("ok_second", read_elsewhere=("mum",))  # another Edition of the Work
    hidden = kobo.volume("above")
    kobo.volume("twelve", live=False)
    for who in (world.kid, world.mum):
        for key in ("ok", "above", "twelve"):
            work = world.seed.works[key]
            first, second = who.get(f"/opds/works/{work}"), who.get(f"{V2}/works/{work}")
            assert first.status_code == second.status_code, (who.name, key)
            if first.status_code == 200:
                assert sorted(x["href"] for x in kepub_links(parse(first))) == sorted(
                    x["href"] for x in v2_kepubs(doc(second))
                ), (who.name, key)  # none missing, none added
    ok = world.seed.works["ok"]
    kid_hrefs = sorted(x["href"] for x in v2_kepubs(doc(world.kid.get(f"{V2}/works/{ok}"))))
    mum_hrefs = [x["href"] for x in v2_kepubs(doc(world.mum.get(f"{V2}/works/{ok}")))]
    assert kid_hrefs == sorted([f"/opds/kepub/{seen}", f"/opds/kepub/{elsewhere}"])
    assert mum_hrefs == [f"/opds/kepub/{seen}"]  # she reads the other one elsewhere
    assert kobo.get(world.kid, seen).status_code == 200
    gone = world.kid.get(f"{V2}/works/{world.seed.works['above']}")
    assert gone.status_code == 404 and hidden not in gone.text


def test_opds2_kepub_the_two_graphs_stay_equivalent(kobo: Kobo) -> None:
    """The meta-test of L19, with KEPUB offers in the feeds: for a child and an adult the 1.2 and
    the 2.0 graphs hold the same entries, titles and links."""
    from tests.integration.test_m5_l19_opds2 import _same_feeds

    kobo.volume("ok")
    kobo.volume("above")
    kobo.volume("ok_second", read_elsewhere=("mum",))
    _same_feeds(kobo.world.kid, "child")
    _same_feeds(kobo.world.mum, "adult")
