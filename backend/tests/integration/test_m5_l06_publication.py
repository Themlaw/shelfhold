"""M5 L06 (EF-LEC-014, EF-LEC-005, EF-USR-006, ENF-SEC-010, D-P1): the publication manifest.

`GET /api/reader/{carrier_type}/{carrier_id}/publication`: what the reader opens, the list of pages
of an image carrier or the reading order of an EPUB, each entry a link into `/content` by ticket.
The real application, real sessions; the archives come from a source the test controls, except for
the bound of an excerpt, which is computed by the real functions of the server (L05a) over the
archive. The attacks: a child on a hidden Work, a key, a device token, no session, an identifier
that is not hers, an entry outside the bound.
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import catalog, export, reading, storage
from shelfhold.library.adapters.epub_excerpt import epub_excerpt
from shelfhold.reading.domain.excerpt import page_excerpt
from tests.content_env import (
    PNG,
    FakeArchive,
    FakeArchives,
    add_carrier,
    session_id_of,
    user_id_of,
    zip_of,
)
from tests.integration.test_l05a_content_route import World, _shape, _world

ABSENT = "01HZZZZZZZZZZZZZZZZZZZZZZZ"
XHTML = "<html xmlns='http://www.w3.org/1999/xhtml'><body><p>{}</p></body></html>"


def epub_entries(
    documents: list[tuple[str, int]], *, direction: str | None = None
) -> dict[str, bytes]:
    """A minimal EPUB: a container, a package whose reading order is `documents` (`name`, size of
    the text), a navigation document, a style sheet and a cover."""
    items = "".join(
        f"<item id='d{i}' href='{name}' media-type='application/xhtml+xml'/>"
        for i, (name, _size) in enumerate(documents)
    )
    refs = "".join(f"<itemref idref='d{i}'/>" for i in range(len(documents)))
    progression = f" page-progression-direction='{direction}'" if direction else ""
    package = (
        "<package xmlns='http://www.idpf.org/2007/opf' version='3.0'>"
        "<metadata xmlns:dc='http://purl.org/dc/elements/1.1/'><dc:language>fr</dc:language>"
        "</metadata><manifest>"
        "<item id='nav' href='nav.xhtml' media-type='application/xhtml+xml' properties='nav'/>"
        "<item id='css' href='style.css' media-type='text/css'/>"
        f"{items}</manifest><spine{progression}>{refs}</spine></package>"
    )
    container = (
        "<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container' version='1.0'>"
        "<rootfiles><rootfile full-path='OEBPS/content.opf' "
        "media-type='application/oebps-package+xml'/></rootfiles></container>"
    )
    out: dict[str, bytes] = {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": container.encode(),
        "OEBPS/content.opf": package.encode(),
        "OEBPS/nav.xhtml": XHTML.format("nav").encode(),
        "OEBPS/style.css": b"p { color: black }",
    }
    for name, size in documents:
        out[f"OEBPS/{name}"] = XHTML.format("x" * size).encode()
    return out


class ServerCutArchives(FakeArchives):
    """The source of the test, with the bound of an excerpt computed by the code of the server."""

    def open(self, carrier_type: str, carrier_id: str, scope: str = "full") -> FakeArchive:
        from shelfhold.reading.application.content_access import (
            ExcerptUnavailableError,
            NoReadableCopyError,
        )

        content_id = (
            self.item_content.get(carrier_id) if carrier_type == "export_item" else carrier_id
        )
        if content_id is None or content_id not in self.by_content:
            raise NoReadableCopyError
        entries = self.by_content[content_id]
        if scope == "excerpt":
            with safe_open_archive(io.BytesIO(zip_of(entries)), name="book.zip") as archive:
                if any(n.endswith(".opf") for n in entries):
                    kept = epub_excerpt(archive)
                else:
                    pages = page_excerpt([n for n in entries if n.endswith(".png")])
                    kept = None if pages is None else frozenset(pages)
            if kept is None:
                raise ExcerptUnavailableError
            entries = {n: b for n, b in entries.items() if n in kept}
        return FakeArchive(entries, self.asked)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _world(monkeypatch, settings)


def unit_of(world: World, content_id: str) -> str:
    with world.seed.engine.connect() as connection:
        return str(
            connection.execute(
                sa.select(storage.content.c.unit_id).where(storage.content.c.id == content_id)
            ).scalar_one()
        )


def book(
    world: World, entries: Mapping[str, bytes], *, edition: str = "ok", fmt: str = "cbz"
) -> str:
    """A kept copy for a Unit of the Edition, served from `entries`; the source cuts excerpts by
    the real functions of the server."""
    content_id = add_carrier(world.seed.engine, world.seed.editions[edition], format=fmt)
    world.src.by_content[content_id] = dict(entries)
    return content_id


def pages_of(count: int, *, first: str = "p") -> dict[str, bytes]:
    return {f"{first}{n}.png": PNG + str(n).encode() for n in range(1, count + 1)}


def publication(client: Any, carrier_type: str, carrier_id: str, **params: Any) -> Any:
    return client.get(f"/api/reader/{carrier_type}/{carrier_id}/publication", params=params)


def entries_of(manifest: dict[str, Any]) -> list[str]:
    """The `/content` paths of a manifest, without the ticket."""
    out = [p["href"] for p in manifest.get("pages", [])] + [
        s["href"] for s in manifest.get("spine", [])
    ]
    return [href.split("/content/", 1)[1].split("/", 1)[1] for href in out]


def fetch(world: World, client: Any, href: str) -> Any:
    return client.get(href[href.index("/content/") :])


def positions(world: World) -> int:
    with world.seed.engine.connect() as connection:
        return int(
            connection.execute(
                sa.select(sa.func.count()).select_from(reading.reading_position)
            ).scalar_one()
        )


# -- the kept copy of a Work that is not exported ----------------------------------------------


@pytest.mark.req("EF-LEC-014", "EF-EXP-058")
def test_publication_of_unexported_work_uses_kept_copy(world: World) -> None:
    """D-P1: a Work that is not exported is read in full from its kept copy (a `content`), pages in
    reading order (p2 before p10), every link a ticket into `/content` that serves its bytes."""
    names = {f"p{n}.png": PNG + str(n).encode() for n in (1, 2, 10, 3)}
    content = book(world, names)
    unit = unit_of(world, content)
    for carrier_type, carrier_id in (("unit", unit), ("content", content)):
        got = publication(world.admin, carrier_type, carrier_id)
        assert got.status_code == 200, (carrier_type, got.text)
        manifest = got.json()
        assert (manifest["carrier_type"], manifest["carrier_id"]) == ("content", content)
        assert manifest["scope"] == "full" and manifest["kind"] == "pages"
        assert entries_of(manifest) == ["p1.png", "p2.png", "p3.png", "p10.png"]
        assert [p["index"] for p in manifest["pages"]] == [1, 2, 3, 4]
        assert manifest["reading_direction"] in {"ltr", "rtl", "webtoon"}
        for page in manifest["pages"]:
            served = fetch(world, world.admin, page["href"])
            assert served.status_code == 200 and served.content.startswith(PNG)
        assert got.headers["cache-control"] == "no-store"  # it holds tickets
    assert "ticket" not in got.text.lower().replace("/content/", "")  # no ticket field, only links


@pytest.mark.req("EF-LEC-014", "EF-EXP-058")
def test_publication_of_exported_unit_chooses_the_exported_file_only_if_read_elsewhere(
    world: World,
) -> None:
    """The kept copy is read in full unless the profile reads the exported file where it was sent
    (a live, verified account on a destination that holds it): then the exported file, and an
    excerpt only. An `export_item` named by hand is an excerpt, never the full book."""
    from tests.integration.test_schema_storage_export import add_destination, add_target

    content = book(world, pages_of(30))
    unit = unit_of(world, content)
    item = world.export_item(world.seed.editions["ok"], content)
    world.src.item_content[item] = content
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.insert(export.unit_coverage).values(
                carrier_type="export_item", carrier_id=item, unit_id=unit, position=1,
                origin="manual",
            )
        )  # fmt: skip
    kept = publication(world.admin, "unit", unit).json()  # exported, but nobody reads it there
    assert (kept["carrier_type"], kept["scope"]) == ("content", "full")
    named = publication(world.admin, "export_item", item)  # by hand: never the whole book
    assert named.status_code == 200 and named.json()["scope"] == "excerpt"
    refused = publication(world.admin, "export_item", item, scope="full")
    assert refused.status_code == 409 and refused.json()["code"] == "full_read_not_allowed"
    assert refused.json().get("action") or refused.json().get("detail")
    # now the admin has a live account on a destination where the file was written
    with write_transaction(world.seed.engine) as connection:
        target = add_target(connection)
        destination = add_destination(connection, world.seed.shelves["family"], target)
        connection.execute(
            sa.text("INSERT OR IGNORE INTO secret_key (kid, state) VALUES ('k1', 'active')")
        )
        import os

        from shelfhold.infrastructure.ids import new_ulid
        from shelfhold.infrastructure.schema import identity

        admin = connection.execute(
            sa.select(identity.user.c.id).where(identity.user.c.username == "admin")
        ).scalar_one()
        connection.execute(
            sa.insert(identity.destination_account).values(
                id=new_ulid(), user_id=admin, shelf_destination_id=destination,
                remote_user="remote-admin", secret_v=1, secret_kid="k1",  # noqa: S106
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
    elsewhere = publication(world.admin, "unit", unit).json()
    assert (elsewhere["carrier_type"], elsewhere["carrier_id"]) == ("export_item", item)
    assert elsewhere["scope"] == "excerpt"
    assert publication(world.admin, "content", content, scope="full").status_code == 409


@pytest.mark.req("EF-LEC-014")
@pytest.mark.parametrize(
    ("work_direction", "expected"),
    [("rtl", "rtl"), ("ltr", "ltr"), ("vertical", "webtoon"), (None, None)],
)
def test_publication_reading_direction_follows_the_series_then_the_mode(
    world: World, work_direction: str | None, expected: str | None
) -> None:
    """The direction the Work sets wins; else the default of its kind (manga right to left, a
    webtoon scrolls); an EPUB without either takes the progression of its own package."""
    content = book(world, pages_of(3))
    work = world.seed.works["ok"]
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.update(catalog.work).where(catalog.work.c.id == work)
            .values(reading_direction=work_direction, kind="comic")
        )  # fmt: skip
    got = publication(world.admin, "content", content).json()["reading_direction"]
    assert got == (expected or "ltr")  # `comic` reads left to right
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.update(catalog.work).where(catalog.work.c.id == work)
            .values(reading_direction=work_direction, kind="manga")
        )  # fmt: skip
    got = publication(world.admin, "content", content).json()["reading_direction"]
    assert got == (expected or "rtl")  # `manga`: the kind's own default
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.update(catalog.work).where(catalog.work.c.id == work)
            .values(reading_direction=work_direction, kind="webtoon")
        )  # fmt: skip
    got = publication(world.admin, "content", content).json()["reading_direction"]
    assert got == (expected or "webtoon")


@pytest.mark.req("EF-LEC-014")
def test_publication_of_an_epub_lists_the_spine_in_reading_order(world: World) -> None:
    entries = epub_entries(
        [("ch2.xhtml", 10), ("ch1.xhtml", 10), ("ch3.xhtml", 10)], direction="rtl"
    )
    content = book(world, entries, fmt="epub")
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.update(catalog.work).where(catalog.work.c.id == world.seed.works["ok"])
            .values(reading_direction=None, kind="novel")
        )  # fmt: skip
    manifest = publication(world.admin, "content", content).json()
    assert manifest["kind"] == "epub" and manifest["scope"] == "full"
    assert [e.rsplit("/", 1)[-1] for e in entries_of(manifest)] == [
        "ch2.xhtml", "ch1.xhtml", "ch3.xhtml",
    ]  # fmt: skip
    assert manifest["reading_direction"] == "rtl" and manifest["language"] == "fr"
    assert manifest["package"].endswith("OEBPS/content.opf")
    assert fetch(world, world.admin, manifest["package"]).status_code == 200
    for item in manifest["spine"]:
        assert fetch(world, world.admin, item["href"]).status_code == 200


# -- what is read: the neutralized derivative --------------------------------------------------


@pytest.mark.req("EF-LEC-005", "EF-BIB-051", "EF-LEC-014")
def test_publication_reads_neutralized_derivative() -> None:
    """ADR-0047, I35 extended: the manifest of an `untrusted` EPUB lists the entries of its
    derivative, never those of the original kept in /store; with no derivative there is nothing
    to list (a typed refusal), never a fall back on the original."""
    from shelfhold.library.public import ReadableCopies
    from shelfhold.reading.application.content_access import NoReadableCopyError
    from shelfhold.reading.application.publication import list_archive
    from tests.integration.test_untrusted_epub_pipeline import TRAPPED, content_row, ingest
    from tests.library_env import lib_env

    with lib_env() as env:
        _result, library = ingest(env)
        row = content_row(env)
        copies = ReadableCopies(env.engine, lambda: env.fsops)
        archive = copies.open("content", row.id)
        try:
            listing = list_archive(archive, "epub")
            assert listing.kind == "epub" and listing.spine, "the derivative has a reading order"
            body = b"".join(archive.read(name) for name in sorted(archive.names()))
            assert b"<script" not in body and b"alert(1)" not in body
            assert all(entry in archive.names() for entry in listing.spine)
        finally:
            archive.close()
        (kept,) = env.files_under(env.store)
        assert kept.read_bytes() == TRAPPED  # the original is in /store ...
        library.write_bytes(TRAPPED)  # ... and even if the library holds it, nothing is listed
        with pytest.raises(NoReadableCopyError):
            copies.open("content", row.id)


# -- « Lire un extrait » (D-P1): bounded by the server -------------------------------------------


@pytest.mark.req("EF-LEC-014", "EF-LEC-005", "EF-USR-006")
def test_excerpt_publication_is_bounded(world: World) -> None:
    # images: a fifth of the pages, ten at the most; the links are the cut, nothing else is served
    content = book(world, pages_of(30))
    manifest = publication(world.admin, "content", content, scope="excerpt").json()
    assert manifest["scope"] == "excerpt"
    assert entries_of(manifest) == [f"p{n}.png" for n in range(1, 7)]  # 30 pages: a fifth is 6
    assert fetch(world, world.admin, manifest["pages"][0]["href"]).status_code == 200
    ticket_part = manifest["pages"][0]["href"].split("/content/", 1)[1].split("/", 1)[0]
    beyond = world.admin.get(f"/content/{ticket_part}/p7.png")  # in the book, not in the cut
    assert beyond.status_code == 404 and "etag" not in beyond.headers
    big = book(world, pages_of(200))
    assert len(entries_of(publication(world.admin, "content", big, scope="excerpt").json())) == 10
    # a book too short to give one page within a fifth has no excerpt: typed, with an action
    short = book(world, pages_of(3))
    refused = publication(world.admin, "content", short, scope="excerpt")
    assert refused.status_code == 409 and refused.json()["code"] == "excerpt_unavailable"
    assert refused.json().get("action") or refused.json().get("detail")
    # an EPUB: the first documents within a fifth of the text, with their resources
    chapters = [(f"c{n}.xhtml", 400) for n in range(1, 12)]
    epub = book(world, epub_entries(chapters), fmt="epub")
    cut = publication(world.admin, "content", epub, scope="excerpt").json()
    assert cut["scope"] == "excerpt" and cut["kind"] == "epub"
    assert [e.rsplit("/", 1)[-1] for e in entries_of(cut)] == ["c1.xhtml", "c2.xhtml"]
    ticket = cut["spine"][0]["href"].split("/content/", 1)[1].split("/", 1)[0]
    assert world.admin.get(f"/content/{ticket}/OEBPS/c3.xhtml").status_code == 404
    # a single-file EPUB, whatever its size: nothing to cut at a document boundary, never the file
    single = book(world, epub_entries([("only.xhtml", 5000)]), fmt="epub")
    one = publication(world.admin, "content", single, scope="excerpt")
    assert one.status_code == 409 and one.json()["code"] == "excerpt_unavailable"
    # a first chapter beyond a fifth of the book: the same typed refusal (ADR-0055 N3)
    heavy = book(world, epub_entries([("c1.xhtml", 3000), ("c2.xhtml", 500), ("c3.xhtml", 500)]),
                 fmt="epub")  # fmt: skip
    assert publication(world.admin, "content", heavy, scope="excerpt").status_code == 409
    # the full read of the same book is still the whole reading order
    full = publication(world.admin, "content", heavy, scope="full").json()
    assert len(full["spine"]) == 3


@pytest.mark.req("EF-LEC-014", "EF-LEC-007")
def test_publication_writes_nothing(world: World) -> None:
    """Reading a manifest, in full or as an excerpt, records no position and no history: an
    excerpt never counts for « Continuer la lecture » nor travels to a reading server."""
    content = book(world, pages_of(30))
    before = positions(world)
    for scope in ("full", "excerpt"):
        assert publication(world.admin, "content", content, scope=scope).status_code == 200
    assert positions(world) == before


# -- attacks -------------------------------------------------------------------------------------


@pytest.mark.req("EF-LEC-014", "EF-USR-006", "ENF-SEC-010")
def test_publication_hidden_work_404_for_child(world: World) -> None:
    """A Work above her ceiling answers a child like a thing that does not exist: the same status,
    the same code, the same length, by the Unit, by the content and by an `export_item` that
    crosses to it; and no ticket is ever issued."""
    hidden = book(world, pages_of(30), edition="twelve")
    unit = unit_of(world, hidden)
    absent = publication(world.kid, "content", ABSENT)
    assert absent.status_code == 404
    crossed = world.export_item(
        world.seed.editions["ok"], hidden
    )  # a visible Edition, a hidden file
    world.src.item_content[crossed] = hidden
    for carrier in (("content", hidden), ("unit", unit), ("export_item", crossed)):
        for scope in ({}, {"scope": "excerpt"}, {"scope": "full"}):
            got = publication(world.kid, *carrier, **scope)
            assert _shape(got) == _shape(absent), (carrier, scope)
            assert "/content/" not in got.text
    # the same Work, read by a profile that may see it, is a manifest
    assert publication(world.mum, "content", hidden).status_code == 200
    # lowered under her feet: a Work reclassified answers the child as absent at the next call
    shown = book(world, pages_of(30))
    assert publication(world.kid, "content", shown).status_code == 200
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.update(catalog.work).where(catalog.work.c.id == world.seed.works["ok"])
            .values(age_rating=16)
        )  # fmt: skip
    assert _shape(publication(world.kid, "content", shown)) == _shape(absent)


@pytest.mark.req("EF-LEC-014", "EF-USR-006", "ENF-SEC-010")
def test_publication_credentials_and_identifiers(world: World) -> None:
    """A key of the API, a device token and no session at all get no ticket; a carrier type or an
    identifier that does not exist is the answer of an absent thing."""
    import base64

    from shelfhold.identity.public import local_auth

    content = book(world, pages_of(30))
    auth = local_auth(world.seed.engine, world.env.settings)
    key = auth.create_api_key(name="reader", username="admin", read_only=True).value
    bare = world.env.other_client("10.0.0.9")
    got = bare.get(
        f"/api/reader/content/{content}/publication", headers={"Authorization": f"Bearer {key}"}
    )
    assert got.status_code in {401, 403} and "/content/" not in got.text
    created = auth.create_device_token(subject_username="mum", name="liseuse")
    raw = base64.b64encode(f"{created.public_id}:{created.secret}".encode()).decode()
    other = world.env.other_client("10.0.0.8")
    got = other.get(
        f"/api/reader/content/{content}/publication", headers={"Authorization": f"Basic {raw}"}
    )
    assert got.status_code in {401, 403} and "/content/" not in got.text
    anonymous = world.env.other_client("10.0.0.7")
    assert publication(anonymous, "content", content).status_code == 401
    for carrier in (("nothing", content), ("content", "not-an-id"), ("unit", ABSENT)):
        assert publication(world.admin, *carrier).status_code == 404, carrier
    assert publication(world.admin, "content", content, scope="everything").status_code == 422


@pytest.mark.req("EF-LEC-014", "EF-USR-006")
def test_publication_ticket_is_the_sessions_own(world: World) -> None:
    """The links of a manifest are bound to the session that asked: another session of the same
    profile, and another profile, get nothing out of them."""
    content = book(world, pages_of(30))
    manifest = publication(world.mum, "content", content).json()
    href = manifest["pages"][0]["href"]
    assert fetch(world, world.mum, href).status_code == 200
    assert fetch(world, world.admin, href).status_code == 404
    again = world.env.other_client("10.0.0.3")
    assert world.env.login("mum", client=again).status_code == 200
    assert fetch(world, again, href).status_code == 404


# -- a Work with no readable copy ----------------------------------------------------------------


@pytest.mark.req("EF-LEC-014")
def test_publication_without_readable_copy_has_action(world: World) -> None:
    """A Unit with no file that may be read (no content, no derivative, a format not read here)
    answers a typed error that says what to do, never an empty manifest."""
    bare = add_carrier(world.seed.engine, world.seed.editions["ok"], format="cbz")  # no archive
    for carrier in (("content", bare), ("unit", unit_of(world, bare))):
        got = publication(world.admin, *carrier)
        assert got.status_code == 409, carrier
        body = got.json()
        assert body["code"] == "no_readable_copy"
        assert body.get("action"), "the answer says what to do"
        assert "/content/" not in got.text
    pdf = add_carrier(world.seed.engine, world.seed.editions["ok"], format="pdf")
    world.src.by_content[pdf] = {"book.pdf": b"%PDF-1.7"}
    assert publication(world.admin, "content", pdf).json()["code"] == "no_readable_copy"


@pytest.mark.req("EF-LEC-014", "EF-LEC-005", "ENF-SEC-004")
def test_publication_links_cannot_climb_out_of_the_ticket_path(world: World) -> None:
    """A name of an archive is the book's, not ours (a CBZ is not neutralized): `%2e%2e`, `?`, `#`
    and `%` in it are encoded in the link, which a browser resolves under `/content/<ticket>/` and
    which the route decodes back to the very name of the archive."""
    from urllib.parse import unquote, urljoin, urlsplit

    odd = ["%2e%2e/%2e%2e/api/session?.png", "a b#c.png", "100%.png", "p1.png"]
    content = book(world, {name: PNG + name.encode() for name in odd})
    manifest = publication(world.admin, "content", content).json()
    assert len(manifest["pages"]) == 4
    ticket = manifest["pages"][0]["href"].split("/content/", 1)[1].split("/", 1)[0]
    for page in manifest["pages"]:
        resolved = urlsplit(urljoin("https://testserver/api/reader/x/y/publication", page["href"]))
        assert resolved.path.startswith(f"/content/{ticket}/"), page["href"]
        assert resolved.query == "" and resolved.fragment == ""
        # what a server decodes (once) is the very name of the archive; the test client decodes
        # twice, so the route is asked through the access service it calls
        entry = unquote(resolved.path.removeprefix(f"/content/{ticket}/"))
        assert entry in odd, page["href"]
        served = world.access.read(
            ticket,
            entry,
            session=(
                user_id_of(world.seed.engine, "admin"),
                session_id_of(world.seed.engine, "admin"),
            ),
        )
        assert served.body == PNG + entry.encode()
    # a name `/content` would refuse is no page of the manifest: it could never be served
    bad = book(world, {"p1.png": PNG, "../x.png": PNG, "a\\b.png": PNG, "ok/p2.png": PNG})
    assert entries_of(publication(world.admin, "content", bad).json()) == ["ok/p2.png", "p1.png"]
