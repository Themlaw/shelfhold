"""L19 (J5-S2, J5-S3, EF-LEC-008, EF-INT-007): one « read elsewhere » query, one answer.

M5 had the rule written three times (J5-S2): `ContentAccess._full_allowed` (by content, verified
account), a near word-for-word copy in `ContinueReading._target` (by the coverage of the Unit) and
`member_keys.live_member_account` (an account with a key, verified or not). Six surfaces were
served from it: the reader, the positions, PSE, KEPUB and « Reprendre ». This lot makes it ONE
query, `content_access.read_elsewhere` — the strictest of the three: by content, verified
account — asked by every surface that applies the rule; PSE follows the bundle rule instead
(D-P7a, written in ADR-0055: the whole volume to a profile the same feed offers the complete file
to); and the `target` of « Reprendre » carries the type of the destination it opens
(« Ouvrir dans Komga », not « Ouvrir dans Kavita » in front of a Komga server).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, identity, storage
from shelfhold.reading.application import content_access, continue_reading
from tests.content_env import add_carrier, user_id_of
from tests.integration.test_l05a_content_route import World, _world
from tests.integration.test_l48_opds import ACQUISITION, ATOM, make_world, parse
from tests.integration.test_m5_l06_publication import ServerCutArchives
from tests.integration.test_m5_l07_positions import HASH, page, put
from tests.integration.test_m5_l14_continue_reading import HOME, items, pulled
from tests.integration.test_m5_l18_pse import PSE_NS, CuttingArchives, Pse, png_pages, stream_links
from tests.integration.test_m5_l20b_kepub_offer import kepub_links


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    """The reader's world: sessions for the administrator, a member and a child."""
    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _world(monkeypatch, settings)


@pytest.fixture
def pse(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Pse]:
    """The OPDS-PSE world, with a session for the child (the reader routes are hers too)."""
    src = CuttingArchives()
    monkeypatch.setattr("shelfhold.main.readable_copies", lambda *_a, **_k: src)
    with make_world(monkeypatch, settings) as found:
        yield Pse(found, src)


def unit_of(world: Any, content_id: str) -> str:
    with world.seed.engine.connect() as connection:
        return str(
            connection.execute(
                sa.select(storage.content.c.unit_id).where(storage.content.c.id == content_id)
            ).scalar_one()
        )


def covered(world: Any, item: str, unit: str) -> None:
    """The export item covers its Unit (the pipeline writes this row; the PSE helper does not)."""
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.insert(export.unit_coverage).values(
                carrier_type="export_item", carrier_id=item, unit_id=unit, position=1,
                origin="manual",
            )
        )  # fmt: skip


def second_content(pse: Pse, unit: str, pages: dict[str, bytes]) -> str:
    """A second kept copy of the same Unit: two contents cover it (J5-S2)."""
    content = new_ulid()
    with write_transaction(pse.world.seed.engine) as connection:
        connection.execute(
            sa.insert(storage.content).values(
                id=content, unit_id=unit, variant_id=None, kind="images",
                media_type="application/vnd.comicbook+zip", format="cbz",
                retention="adopted_ref", path=None, original_sha256="sha256:" + "0" * 64,
                store_provenance="received", size=1,
                content_hash=HASH, content_hash_by="norm@1",
            )
        )  # fmt: skip
    pse.src.by_content[content] = dict(pages)
    return content


def sent(
    world: Any,
    content: str | None,
    unit: str,
    *,
    readers: tuple[str, ...],
    edition: str = "ok",
    kind: str = "kavita",
    url: str | None = None,
    state: str = "written",
) -> str:
    """The content (or a Unit that only exists as a file, `content=None`) is written to a
    destination of `kind` where each reader has a live, verified account: the export item."""
    from tests.integration.test_schema_storage_export import add_destination

    item = new_ulid()
    with write_transaction(world.seed.engine) as connection:
        target = new_ulid()
        connection.execute(
            sa.insert(export.export_target).values(
                id=target, kind=kind, name=f"{kind}-{new_ulid()}",
                config={"url": url} if url else {},
            )
        )  # fmt: skip
        destination = add_destination(connection, world.seed.shelves["adult"], target)
        connection.execute(
            sa.insert(export.export_item).values(
                id=item, edition_id=world.seed.editions[edition], kind="unit", content_id=content,
            )
        )  # fmt: skip
        connection.execute(
            sa.insert(export.unit_coverage).values(
                carrier_type="export_item", carrier_id=item, unit_id=unit, position=1,
                origin="manual",
            )
        )  # fmt: skip
        connection.execute(
            sa.text("INSERT OR IGNORE INTO secret_key (kid, state) VALUES ('k1', 'active')")
        )
        for name in readers:
            connection.execute(
                sa.insert(identity.destination_account).values(
                    id=new_ulid(), user_id=user_id_of(world.seed.engine, name),
                    shelf_destination_id=destination, remote_user=f"remote-{name}",
                    secret_v=1, secret_kid="k1",  # noqa: S106 (a key id)
                    secret_nonce=os.urandom(12), secret_ct=os.urandom(32),
                    token_fingerprint="f" * 12, verified_at=sa.func.current_timestamp(),
                )
            )  # fmt: skip
        connection.execute(
            sa.insert(export.export_record).values(
                shelf_destination_id=destination, export_item_id=item, locator_type="none",
                state=state,
            )
        )  # fmt: skip
    return item


def session_of(world: Any, name: str, ip: str) -> Any:
    """A logged-in session for a profile of the world (the OPDS world keeps the child's only)."""
    client = world.env.other_client(ip)
    assert world.env.login(name, client=client).status_code == 200
    return client


# -- one query, asked by every surface (J5-S2) -------------------------------------------------


@pytest.mark.req("EF-LEC-008", "EF-INT-007")
def test_read_elsewhere_single_query_used_by_every_surface(pse: Pse, monkeypatch: Any) -> None:
    """The rule is written once: no module of the reading package joins the export ledger with
    the accounts but `content_access`, and every surface that applies the rule — the reader, the
    positions, KEPUB, PSE and « Reprendre » — asks `content_access.read_elsewhere` (by content,
    verified account), never a copy of its own."""
    package = Path(content_access.__file__).parent.parent
    for path in sorted(package.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "destination_account" in text:
            assert "export_record" not in text or path.name == "content_access.py", path

    world = pse.world
    pages = png_pages(30)
    item = pse.stream("ok", pages, read_elsewhere=("kid",))  # the child reads this volume there
    content = pse.src.item_content[item]
    unit = unit_of(world, content)
    covered(world, item, unit)
    epub = add_carrier(world.seed.engine, world.seed.editions["twelve"], format="epub")
    epub_item = sent(world, epub, unit_of(world, epub), readers=("kid",), edition="twelve")

    calls = {"n": 0}
    real = content_access.read_elsewhere

    def spy(connection: Any, *, user_id: str, content_id: str) -> str | None:
        calls["n"] += 1
        return real(connection, user_id=user_id, content_id=content_id)

    monkeypatch.setattr(content_access, "read_elsewhere", spy)
    monkeypatch.setattr(continue_reading, "read_elsewhere", spy)

    seen = calls["n"]  # the reader: the Unit is an excerpt for her, read where it was sent
    manifest = world.kid_session.get(f"/api/reader/unit/{unit}/publication")
    assert manifest.status_code == 200, manifest.text
    assert (manifest.json()["scope"], manifest.json()["carrier_type"]) == (
        "excerpt", "export_item",
    )  # fmt: skip
    assert calls["n"] > seen
    seen = calls["n"]  # the positions: an excerpt writes nothing
    assert put(world, world.kid_session, content, page(3)).status_code == 409
    assert calls["n"] > seen
    seen = calls["n"]  # KEPUB: judged by the same query (a Work with no stream, so the count is
    feed = parse(world.mum.get(f"/opds/works/{world.seed.works['twelve']}"))  # KEPUB's alone)
    assert [link["href"] for link in kepub_links(feed)] == [f"/opds/kepub/{epub_item}"]
    assert calls["n"] > seen
    seen = calls["n"]  # PSE: the stream of the volume, judged by the same query
    assert pse.page(world.kid, item, 0).status_code == 200
    assert calls["n"] > seen
    pulled(world, "kid", "export_item", item, done=False)
    seen = calls["n"]  # « Reprendre »: the entry opens where the Unit was sent
    got = world.kid_session.get(HOME).json()
    assert [(i["unit_id"], i["target"]) for i in got["items"]] == [(unit, "kavita")]
    assert calls["n"] > seen


@pytest.mark.req("EF-LEC-008", "EF-INT-007")
def test_unit_covered_by_two_contents_read_elsewhere_consistent(pse: Pse) -> None:
    """A Unit covered by two contents — one sent where the child reads it, one kept — has one
    answer everywhere: the kept one is read here in full (the reader, the positions and
    « Reprendre » point at it), the sent one is an excerpt in the reader for her and a full read
    for a member without an account there, and PSE serves the whole volume (the bundle rule)."""
    world = pse.world
    pages = png_pages(30)
    item = pse.stream("ok", pages, read_elsewhere=("kid",))
    gone = pse.src.item_content[item]  # sent where the child reads it
    unit = unit_of(world, gone)
    covered(world, item, unit)
    kept = second_content(pse, unit, pages)  # never sent: hers to read in full
    member = session_of(world, "mum", "10.0.0.31")

    manifest = world.kid_session.get(f"/api/reader/unit/{unit}/publication")
    assert manifest.status_code == 200, manifest.text
    found = manifest.json()
    assert (found["carrier_type"], found["carrier_id"], found["scope"]) == (
        "content", kept, "full",
    )  # fmt: skip
    kid_sent = world.kid_session.get(f"/api/reader/content/{gone}/publication")
    assert kid_sent.status_code == 200 and kid_sent.json()["scope"] == "excerpt"
    mum_sent = member.get(f"/api/reader/content/{gone}/publication")
    assert mum_sent.status_code == 200 and mum_sent.json()["scope"] == "full"
    assert put(world, world.kid_session, kept, page(3)).status_code == 200  # the positions agree
    assert put(world, world.kid_session, gone, page(3)).status_code == 409
    pulled(world, "kid", "export_item", item, done=False)  # she reads it there: a server position
    got = items(world.kid_session.get(HOME))  # « Reprendre » points at the kept copy
    assert [(i["unit_id"], i["target"]) for i in got] == [(unit, "kept_copy")]
    assert (got[0]["carrier_type"], got[0]["carrier_id"]) == ("content", kept)
    (kid_link,) = stream_links(  # PSE: the whole volume to her too (D-P7a, ADR-0055)
        parse(world.kid.get(f"/opds/works/{world.seed.works['ok']}"))
    )
    assert int(kid_link[f"{PSE_NS}count"]) == 30
    assert pse.page(world.kid, item, 29).status_code == 200


@pytest.mark.req("EF-LEC-008")
def test_continue_reading_a_unit_with_no_content_here_is_not_read_elsewhere(world: World) -> None:
    """The query judges contents (the strictest of the three, J5-S2): a Unit whose file only
    exists as an export — no kept copy here, no content of hers to judge — is not « read
    elsewhere »; the entry is dropped rather than sent to a destination nothing of hers is
    proven on, and the series waits for a Unit that can be opened."""
    from tests.integration.test_schema_catalog import add_unit

    with write_transaction(world.seed.engine) as connection:
        unit = add_unit(connection, world.seed.editions["ok"], "500")
    item = sent(world, None, unit, readers=("admin",))
    pulled(world, "admin", "export_item", item, done=False)
    assert items(world.admin.get(HOME)) == []  # nothing of hers is proven on that destination


# -- the label names the destination (J5-S2) ---------------------------------------------------


@pytest.mark.req("EF-LEC-008")
def test_continue_reading_label_names_destination_type(world: World) -> None:
    """`target` carries the type of the destination: a Unit read on a Komga server opens
    « Ouvrir dans Komga », at that server's address — not Kavita's label, not Kavita's address;
    a member without an account there reads the kept copy in full (another's account is not
    hers, and the answer never tells which of the two it is)."""
    from tests.integration.test_m5_l07_positions import kept

    content, unit = kept(world)
    item = sent(
        world, content, unit, readers=("admin",), kind="komga", url="https://komga.test:5000"
    )
    pulled(world, "admin", "export_item", item, done=False)
    got = world.admin.get(HOME).json()
    assert [(i["unit_id"], i["target"]) for i in got["items"]] == [(unit, "komga")]
    assert got["open_urls"] == {"komga": "https://komga.test:5000"}
    assert (got["items"][0]["carrier_type"], got["items"][0]["carrier_id"]) == (None, None)
    pulled(world, "mum", "export_item", item, done=False)
    hers = items(world.mum.get(HOME))
    assert [(i["unit_id"], i["target"]) for i in hers] == [(unit, "kept_copy")]
    assert (hers[0]["carrier_type"], hers[0]["carrier_id"]) == ("content", content)


# -- PSE follows the bundle rule (J5-S3, D-P7a, ADR-0055) ---------------------------------------


@pytest.mark.req("EF-INT-007")
def test_pse_follows_bundle_rule_per_adr_0055(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """The same feed offers the complete file of the series to the profile, so the PSE stream
    serves the whole volume — to the profile who reads it elsewhere too. The bound of the web
    reader's excerpt (D-P1) bounds nothing here: it is a rule of the reader, not of the feed."""
    from tests.integration.test_l48_opds import FakeBundles

    src = CuttingArchives()
    monkeypatch.setattr("shelfhold.main.readable_copies", lambda *_a, **_k: src)
    with make_world(monkeypatch, settings, FakeBundles()) as found:
        world = Pse(found, src)
        pages = png_pages(30)
        item = world.stream("ok", pages, read_elsewhere=("kid",))
        feed = parse(world.world.kid.get(f"/opds/works/{world.world.seed.works['ok']}"))
        complete = [
            link.attrib.get("href")
            for link in feed.iter(f"{ATOM}link")
            if link.attrib.get("rel") == ACQUISITION and "/complete" in link.attrib.get("href", "")
        ]
        assert complete  # the block of the series is offered to the very same profile
        (kid_link,) = stream_links(feed)
        assert int(kid_link[f"{PSE_NS}count"]) == 30  # the whole volume, not the cut of an excerpt
        for n in (0, 29):
            got = world.page(world.world.kid, item, n)
            assert got.status_code == 200 and got.content == pages[f"p{n + 1}.png"]
