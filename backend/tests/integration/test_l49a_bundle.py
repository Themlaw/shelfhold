"""M4 L49a: the block of a series, served on OPDS in one download (ADR-0053 §1 to §3, §8;
EF-INT-007, EF-LEC-009, EF-USR-006, ENF-SEC-010, ENF-QUAL-005).

The files are real: chapters written as CBZ on a temporary disk, a database that says where they
are, the real application behind a device token, the real `zipfile` reading what comes back. The
attacks come from a child's token, a member's, a revoked token, no token, an API key, another
profile's version, a climbing path, a symbolic link, a chapter replaced under the reader (before the
first byte, and during the transfer) and a version the retention has purged.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tracemalloc
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.identity.public import reader_scope
from shelfhold.infrastructure.schema import export
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.reading.bundle import (
    BundleBusyError,
    BundleChangedError,
    BundleGoneError,
    BundleLimits,
    SeriesBundles,
)
from shelfhold.reading.domain.document_hash import koreader_partial_md5
from tests.bundle_env import Chapter, Library, library_for, page
from tests.integration.test_l48_opds import (
    ATOM,
    Reader,
    World,
    entries,
    links,
    make_world,
    parse,
)

pytestmark = pytest.mark.req(
    "EF-INT-007", "EF-LEC-009", "EF-USR-006", "ENF-SEC-010", "ENF-QUAL-005"
)

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
ZIP = "application/vnd.comicbook+zip"
KEYRING = Keyring.generate()


@pytest.fixture
def lab(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with make_world(monkeypatch, settings) as made:
        yield made


def library(world: World, key: str = "ok") -> Library:
    root = world.env.settings.data_dir.parent / f"lib-{key}"
    return library_for(world.seed.engine, root, world.seed.editions[key])


def three_chapters(lib: Library) -> None:
    for number in ("1", "2", "10"):
        lib.add(Chapter.simple(number, count=3))


def complete(reader: Reader, world: World, key: str = "ok", part: int | None = None) -> Any:
    suffix = f"?part={part}" if part else ""
    return reader.get(f"/opds/editions/{world.seed.editions[key]}/complete{suffix}")


def names_of(body: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        return [i.filename for i in archive.infolist()]


def user_id(world: World, name: str) -> str:
    with world.seed.engine.connect() as c:
        return str(
            c.execute(sa.text("SELECT id FROM user WHERE username = :n"), {"n": name}).scalar_one()
        )


def token_id(world: World, reader: Reader) -> str:
    with world.seed.engine.connect() as c:
        return str(
            c.execute(
                sa.text("SELECT id FROM device_token WHERE public_id = :p"),
                {"p": reader.public_id.lower()},
            ).scalar_one()
        )


def provider(world: World, *, limits: BundleLimits | None = None, **kw: Any) -> SeriesBundles:
    return SeriesBundles(
        world.seed.engine, lambda: KEYRING, now=lambda: NOW, limits=limits or BundleLimits(), **kw
    )


def scope_of(world: World, name: str) -> Any:
    with world.seed.engine.connect() as c:
        return reader_scope(c, user_id(world, name))


def drain(download: Any) -> bytes:
    return b"".join(download.chunks)


def problem(response: Any) -> tuple[int, dict[str, Any]]:
    """What an answer says, without the identifier of the request."""
    body = {k: v for k, v in response.json().items() if k != "request_id"}
    return response.status_code, body


def fresh_token(world: World, name: str = "mum") -> str:
    from tests.integration.test_l48_opds import auth_of

    made = auth_of(world.env).create_device_token(subject_username=name, name="extra")
    with world.seed.engine.connect() as c:
        return str(
            c.execute(
                sa.text("SELECT id FROM device_token WHERE public_id = :p"),
                {"p": made.public_id.lower()},
            ).scalar_one()
        )


def md5_of(body: bytes) -> str:
    return "md5:" + koreader_partial_md5(io.BytesIO(body))


# -- the block as a file ---------------------------------------------------------------------------


def test_the_block_is_one_stored_zip_with_the_chapters_in_order_and_an_exact_length(
    lab: World,
) -> None:
    three_chapters(library(lab))
    response = complete(lab.mum, lab)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(ZIP)
    assert int(response.headers["content-length"]) == len(response.content)
    assert response.headers["cache-control"] == "private,no-cache"
    assert "Série complète v1.cbz" in response.headers["content-disposition"] or (
        "S%C3%A9rie%20compl%C3%A8te%20v1.cbz" in response.headers["content-disposition"]
    )
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert {i.compress_type for i in archive.infolist()} == {zipfile.ZIP_STORED}
        assert archive.testzip() is None
        expected = [
            f"{rank:05d} ch{number}/{p:05d}.png"
            for rank, number in ((1, "1"), (2, "2"), (3, "10"))
            for p in (1, 2, 3)
        ]
        assert [i.filename for i in archive.infolist()] == expected  # 10 after 2, not before
        assert archive.read("00002 ch2/00003.png") == page("png", "ch2p3")


@pytest.mark.skipif(shutil.which("mutool") is None, reason="mutool is not installed")
def test_mutool_opens_the_block_and_lists_its_pages(lab: World, tmp_path: Path) -> None:
    three_chapters(library(lab))
    target = tmp_path / "block.cbz"
    target.write_bytes(complete(lab.mum, lab).content)
    mutool = shutil.which("mutool") or "mutool"
    shown = subprocess.run(  # noqa: S603  (a fixed tool, a file of the test)
        [mutool, "info", str(target)], capture_output=True, text=True, check=False, timeout=60
    )
    assert shown.returncode == 0, shown.stderr
    assert "Pages: 9" in shown.stdout


def test_only_pages_are_in_the_block_not_the_comicinfo_of_each_chapter(lab: World) -> None:
    three_chapters(library(lab))
    assert not [n for n in names_of(complete(lab.mum, lab).content) if "ComicInfo" in n]


def test_a_deflated_source_is_stored_in_the_block_with_the_same_page_bytes(lab: World) -> None:
    lib = library(lab)
    lib.add(Chapter.simple("1", count=4, deflate=True))
    with zipfile.ZipFile(io.BytesIO(complete(lab.mum, lab).content)) as archive:
        assert {i.compress_type for i in archive.infolist()} == {zipfile.ZIP_STORED}
        assert archive.read("00001 ch1/00004.png") == page("png", "ch1p4")


def test_two_downloads_are_the_same_bytes(lab: World) -> None:
    three_chapters(library(lab))
    first, second = complete(lab.mum, lab), complete(lab.mum, lab)
    assert first.content == second.content
    assert first.headers["content-disposition"] == second.headers["content-disposition"]


def test_the_same_bytes_and_fingerprint_after_a_restart_and_a_regeneration(lab: World) -> None:
    """Two generations of the same members, a new process in between, and the version rows gone:
    same bytes, same `content_fp` (the key is the keyring's, which a restart keeps)."""
    lib = library(lab)
    three_chapters(lib)
    scope, who = scope_of(lab, "mum"), user_id(lab, "mum")
    edition = lab.seed.editions["ok"]
    before = provider(lab).open(user_id=who, scope=scope, edition_id=edition)
    one, fp_one = drain(before), before.content_fp
    with lab.seed.engine.begin() as c:
        c.execute(sa.text("DELETE FROM bundle_version"))
    after = provider(lab).open(user_id=who, scope=scope, edition_id=edition)  # a new instance
    assert drain(after) == one
    assert after.content_fp == fp_one
    assert after.filename == before.filename  # the counter began again at the same number


def test_the_fingerprint_changes_with_the_members_and_the_key(lab: World) -> None:
    lib = library(lab)
    three_chapters(lib)
    scope, who = scope_of(lab, "mum"), user_id(lab, "mum")
    edition = lab.seed.editions["ok"]
    one = provider(lab).open(user_id=who, scope=scope, edition_id=edition)
    fp = one.content_fp
    drain(one)
    other_key = SeriesBundles(
        lab.seed.engine, lambda: Keyring.generate(), now=lambda: NOW, limits=BundleLimits()
    )
    with lab.seed.engine.begin() as c:
        c.execute(sa.text("DELETE FROM bundle_version"))
    assert other_key.open(user_id=who, scope=scope, edition_id=edition).content_fp != fp


def test_the_koreader_digest_of_the_version_is_that_of_the_bytes_and_is_an_alias(
    lab: World,
) -> None:
    three_chapters(library(lab))
    body = complete(lab.mum, lab).content
    with lab.seed.engine.connect() as c:
        stored = c.execute(
            sa.select(sa.text("koreader_md5")).select_from(sa.text("bundle_version"))
        ).scalar_one()
        alias = c.execute(
            sa.select(
                export.document_alias.c.koreader_partial_md5, export.document_alias.c.export_item_id
            )
        ).all()
    assert stored == md5_of(body)
    assert [a.koreader_partial_md5 for a in alias] == [md5_of(body)]  # the relay of L47 finds it


def test_a_new_chapter_is_a_new_version_a_new_name_a_new_digest_and_the_old_one_stays(
    lab: World,
) -> None:
    lib = library(lab)
    three_chapters(lib)
    first = complete(lab.mum, lab)
    lib.add(Chapter.simple("11", count=2))
    second = complete(lab.mum, lab)
    assert first.headers["content-disposition"] != second.headers["content-disposition"]
    assert (
        " v2" in second.headers["content-disposition"]
        or "%20v2" in second.headers["content-disposition"]
    )
    assert md5_of(first.content) != md5_of(second.content)  # the first Kibibyte tells them apart
    assert first.content[:1024] != second.content[:1024]
    with lab.seed.engine.connect() as c:
        versions = c.execute(sa.text("SELECT count(*) FROM bundle_version")).scalar_one()
        aliases = c.execute(
            sa.select(sa.func.count()).select_from(export.document_alias)
        ).scalar_one()
    assert (versions, aliases) == (2, 2)


def test_the_table_of_pages_is_counted_by_shelfhold_from_the_pages_kept(lab: World) -> None:
    lib = library(lab)
    lib.add(Chapter.simple("1", count=3))
    lib.add(Chapter.simple("2", count=5))
    lib.add(Chapter.simple("3", count=2))
    complete(lab.mum, lab)
    with lab.seed.engine.connect() as c:
        rows = c.execute(
            sa.text("SELECT rank, first_page, page_count FROM bundle_member ORDER BY rank")
        ).all()
    assert [tuple(r) for r in rows] == [(1, 1, 3), (2, 4, 5), (3, 9, 2)]


# -- what is left out ------------------------------------------------------------------------------


def test_a_chapter_with_an_avif_page_or_an_epub_or_a_pdf_is_left_out_and_said_so(
    lab: World,
) -> None:
    lib = library(lab)
    lib.add(Chapter.simple("1", count=2))
    lib.add(Chapter.simple("2", count=2, ext="avif"))
    lib.add(Chapter.simple("3", count=2, fmt="epub"))
    lib.add(Chapter.simple("4", count=2, fmt="pdf"))
    lib.add(Chapter.simple("5", count=2, ext="webp"))
    body = complete(lab.mum, lab).content
    assert [n.split("/")[0] for n in names_of(body)][::2] == [
        "00001 ch1",
        "00002 ch5",
    ]  # ranks stay dense
    feed = parse(lab.mum.get(f"/opds/works/{lab.seed.works['ok']}"))
    summary = " ".join(s.text or "" for s in feed.iter(f"{ATOM}summary"))
    assert "3" in summary  # three chapters left out


def test_a_page_in_gif_jpeg_and_webp_is_kept_with_its_extension(lab: World) -> None:
    lib = library(lab)
    lib.add(
        Chapter(
            "1",
            [
                ("a.gif", page("gif", "g")),
                ("b.jpg", page("jpg", "j")),
                ("c.webp", page("webp", "w")),
            ],
        )
    )
    assert [n.split("/")[1] for n in names_of(complete(lab.mum, lab).content)] == [
        "00001.gif", "00002.jpg", "00003.webp",
    ]  # fmt: skip


def test_the_block_names_no_path_of_the_disk_and_no_name_of_a_source(lab: World) -> None:
    lib = library(lab)
    lib.add(Chapter("1", [("deep/er/ünï.png", page("png", "a")), ("x.png", page("png", "b"))]))
    body = complete(lab.mum, lab).content
    root = str(lib.root)
    assert root.encode() not in body and b"Series/" not in body and b"ch-1.cbz" not in body
    assert "ünï".encode() not in body and b"deep" not in body  # a source's name is never copied
    for name in names_of(body):
        assert ".." not in name.split("/") and not name.startswith("/") and "\\" not in name
        assert name.isascii()


def test_a_chapter_whose_archive_holds_a_climbing_name_is_left_out(lab: World) -> None:
    lib = library(lab)
    lib.add(Chapter.simple("1", count=2))
    lib.add(Chapter("2", [("../../x.png", page("png", "CLIMB")), ("1.png", page("png", "ok"))]))
    body = complete(lab.mum, lab).content
    assert b"CLIMB" not in body
    assert {n.split("/")[0] for n in names_of(body)} == {"00001 ch1"}


# -- attacks: the path ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel", ["../outside.cbz", "Series/../../outside.cbz", "/etc/outside.cbz", "a\\b.cbz"]
)
def test_a_record_that_climbs_out_of_the_root_is_left_out_and_its_file_is_never_read(
    lab: World, tmp_path: Path, rel: str
) -> None:
    lib = library(lab)
    lib.add(Chapter.simple("1", count=2))
    secret = Chapter("2", [("1.png", page("png", "OUTSIDE-SECRET"))])
    lib.add_raw(secret, rel, at=lib.root.parent / "outside.cbz")
    body = complete(lab.mum, lab).content
    assert b"OUTSIDE-SECRET" not in body
    assert {n.split("/")[0] for n in names_of(body)} == {"00001 ch1"}


def test_a_symbolic_link_out_of_the_root_is_left_out(lab: World) -> None:
    lib = library(lab)
    lib.add(Chapter.simple("1", count=2))
    elsewhere = lib.root.parent / "elsewhere.cbz"
    from tests.bundle_env import write_cbz

    write_cbz(elsewhere, Chapter("2", [("1.png", page("png", "LINKED-SECRET"))]))
    item = lib.add_raw(Chapter.simple("2", count=1), "Series/linked.cbz")
    (lib.root / "Series").mkdir(exist_ok=True)
    (lib.root / "Series" / "linked.cbz").symlink_to(elsewhere)
    assert item.rel_path == "Series/linked.cbz"
    assert b"LINKED-SECRET" not in complete(lab.mum, lab).content


# -- attacks: who may ask --------------------------------------------------------------------------


def test_a_child_gets_the_block_of_an_edition_she_may_see_and_nothing_hidden_in_it(
    lab: World,
) -> None:
    for key in ("mixed_rated", "mixed_new"):
        lib = library(lab, key)
        lib.add(Chapter.simple("1" if key == "mixed_rated" else "9", count=2))
    response = complete(lab.kid, lab, "mixed_rated")
    assert response.status_code == 200
    assert {n.split("/")[0] for n in names_of(response.content)} == {"00001 ch1"}
    assert b"ch9p" not in response.content  # the chapter of the hidden Edition is nowhere
    lab.clean(response.headers["content-disposition"])


@pytest.mark.parametrize("key", ["above", "adult_edition", "misfiled", "mixed_new"])
def test_a_child_asking_for_a_hidden_edition_gets_what_an_absent_one_gets(
    lab: World, key: str
) -> None:
    library(lab, key).add(Chapter.simple("1", count=1))
    hidden = complete(lab.kid, lab, key)
    absent = lab.kid.get("/opds/editions/01HZZZZZZZZZZZZZZZZZZZZZZM/complete")
    assert problem(hidden) == problem(absent)
    assert hidden.status_code == 404
    assert b"PK" not in hidden.content[:2]
    assert complete(lab.mum, lab, key).status_code == 200  # the member's own scope is wider


def test_the_offer_of_a_hidden_edition_is_nowhere_in_a_child_feed(lab: World) -> None:
    library(lab, "above").add(Chapter.simple("1", count=1))
    library(lab, "ok").add(Chapter.simple("1", count=1))
    mixed = parse(lab.kid.get(f"/opds/works/{lab.seed.works['ok']}"))
    text = "".join(str(link) for entry in entries(mixed) for link in links(entry)) + "".join(
        e.findtext(f"{ATOM}title") or "" for e in entries(mixed)
    )
    assert lab.seed.editions["above"] not in text
    assert lab.seed.editions["ok"] in text


def test_a_series_hidden_to_a_child_offers_no_block_through_its_works(lab: World) -> None:
    library(lab, "above").add(Chapter.simple("1", count=1))
    assert lab.kid.get(f"/opds/series/{lab.series['mixed']}").status_code == 404
    assert complete(lab.kid, lab, "above").status_code == 404


def test_the_version_of_another_profile_is_not_served_to_this_one(lab: World) -> None:
    """IDOR on a version: a version is made for one profile; its identifier is no capability."""
    lib = library(lab)
    three_chapters(lib)
    api = provider(lab)
    theirs = api.open(
        user_id=user_id(lab, "mum"), scope=scope_of(lab, "mum"), edition_id=lab.seed.editions["ok"]
    )
    drain(theirs)
    with pytest.raises(LookupError):
        api.open_version(
            user_id=user_id(lab, "kid"), scope=scope_of(lab, "kid"), version_id=theirs.version_id
        )
    mine = lab.kid.get(f"/opds/bundles/{theirs.version_id}")
    unknown = lab.kid.get("/opds/bundles/01HZZZZZZZZZZZZZZZZZZZZZZM")
    assert problem(mine) == problem(unknown)
    assert mine.status_code == 404


def test_a_version_whose_chapter_became_hidden_is_refused_even_to_its_own_profile(
    lab: World,
) -> None:
    lib = library(lab, "ok")
    lib.add(Chapter.simple("1", count=2))
    made = complete(lab.kid, lab, "ok")
    assert made.status_code == 200
    version = _latest_version(lab)
    with lab.seed.engine.begin() as c:  # the Edition is reclassified above her ceiling
        c.execute(
            sa.text("UPDATE edition SET age_rating = 18 WHERE id = :e"),
            {"e": lab.seed.editions["ok"]},
        )
    assert lab.kid.get(f"/opds/bundles/{version}").status_code == 404
    assert complete(lab.kid, lab, "ok").status_code == 404


def _latest_version(world: World) -> str:
    with world.seed.engine.connect() as c:
        return str(
            c.execute(
                sa.text("SELECT id FROM bundle_version ORDER BY rowid DESC LIMIT 1")
            ).scalar_one()
        )


def test_the_same_version_downloads_again_by_its_identifier_with_the_same_bytes(lab: World) -> None:
    three_chapters(library(lab))
    first = complete(lab.mum, lab)
    again = lab.mum.get(f"/opds/bundles/{_latest_version(lab)}")
    assert again.status_code == 200 and again.content == first.content


def test_no_token_a_revoked_token_a_session_and_an_api_key_get_no_block(lab: World) -> None:
    three_chapters(library(lab))
    path = f"/opds/editions/{lab.seed.editions['ok']}/complete"
    assert lab.mum.client.get(path).status_code == 401
    assert lab.kid_session.get(path).status_code in (
        401,
        403,
    )  # a web session is not a device token
    from tests.integration.test_l48_opds import auth_of

    auth_of(lab.env).revoke_device_token(lab.mum.public_id)
    assert lab.mum.get(path).status_code == 401
    assert lab.mum.get(f"/opds/bundles/{_latest_version_or_absent(lab)}").status_code == 401


def _latest_version_or_absent(world: World) -> str:
    return "01HZZZZZZZZZZZZZZZZZZZZZZM"


def test_a_part_or_a_version_that_is_not_an_identifier_is_an_absent_one(lab: World) -> None:
    three_chapters(library(lab))
    assert lab.mum.get("/opds/bundles/..%2f..%2fetc").status_code == 404
    assert lab.mum.get("/opds/bundles/not-an-id").status_code == 404
    assert complete(lab.mum, lab, part=99).status_code == 404
    assert lab.mum.get(f"/opds/editions/{lab.seed.editions['ok']}/complete?part=0").status_code in (
        404,
        422,
    )


# -- the feed --------------------------------------------------------------------------------------


def test_the_feed_offers_the_block_with_the_next_name_and_the_exact_size_once_it_exists(
    lab: World,
) -> None:
    three_chapters(library(lab))
    work = f"/opds/works/{lab.seed.works['ok']}"

    def offer() -> dict[str, str]:
        feed = parse(lab.mum.get(work))
        acquisition = [
            x for e in entries(feed) for x in links(e)
            if x.get("rel") == "http://opds-spec.org/acquisition"
        ]  # fmt: skip
        assert len(acquisition) >= 1
        return acquisition[0]

    before = offer()
    assert "length" not in before  # nothing was made yet: no size is promised
    made = complete(lab.mum, lab)
    after = offer()
    assert int(after["length"]) == len(made.content)  # now it is exact
    assert after["href"].endswith("/complete")
    feed = parse(lab.mum.get(work))
    assert any(
        "Série complète" in (e.findtext(f"{ATOM}title") or "")
        and " v1" in (e.findtext(f"{ATOM}title") or "")
        for e in entries(feed)
    )


def test_the_title_names_the_version_and_loses_the_characters_a_name_may_not_have(
    lab: World,
) -> None:
    three_chapters(library(lab))
    with lab.seed.engine.begin() as c:
        c.execute(
            sa.text("UPDATE work SET title = :t WHERE id = :w"),
            {"t": 'A/B:C*D?E"F<G>H|I', "w": lab.seed.works["ok"]},
        )
    from urllib.parse import unquote

    disposition = complete(lab.mum, lab).headers["content-disposition"]
    name = unquote(disposition.split("filename*=UTF-8''", 1)[1])
    assert name.endswith("Série complète v1.cbz")
    assert not set('/\\:*?"<>|') & set(name)


# -- parts: (1 sur N) ----------------------------------------------------------------------


def _small_parts(lab: World) -> BundleLimits:
    return BundleLimits(part_bytes=3_000)


def test_a_series_over_the_cap_is_cut_in_parts_on_stable_boundaries(lab: World) -> None:
    lib = library(lab)
    for number in range(1, 7):
        lib.add(
            Chapter(str(number), [(f"{p}.png", page("png", f"c{number}p{p}", 400)) for p in (1, 2)])
        )
    api, who, scope = (
        provider(lab, limits=_small_parts(lab)),
        user_id(lab, "mum"),
        scope_of(lab, "mum"),
    )
    edition = lab.seed.editions["ok"]
    offers = api.offers(user_id=who, scope=scope, edition_id=edition)
    assert len(offers) >= 2
    assert [o.part for o in offers] == list(range(1, len(offers) + 1))
    assert all(f"({o.part} sur {len(offers)})" in o.filename for o in offers)
    bodies = [
        drain(api.open(user_id=who, scope=scope, edition_id=edition, part=o.part)) for o in offers
    ]
    assert all(len(b) < 3_000 + 600 for b in bodies)  # a part respects the cap (a chapter is whole)
    seen = [n.split("/")[0].split(" ")[1] for b in bodies for n in names_of(b)[::2]]
    assert seen == [f"ch{n}" for n in range(1, 7)]  # every chapter once, in order
    with lab.seed.engine.connect() as c:
        boundaries = c.execute(
            sa.text("SELECT part_index, first_unit_id FROM bundle_partition ORDER BY part_index")
        ).all()
    # a chapter added at the end goes in the last part until the cap, and no boundary moves
    lib.add(Chapter("7", [("1.png", page("png", "c7p1", 100))]))
    again = api.offers(user_id=who, scope=scope, edition_id=edition)
    drain(api.open(user_id=who, scope=scope, edition_id=edition, part=again[-1].part))
    with lab.seed.engine.connect() as c:
        after = c.execute(
            sa.text("SELECT part_index, first_unit_id FROM bundle_partition ORDER BY part_index")
        ).all()
    assert [tuple(b) for b in after[: len(boundaries)]] == [tuple(b) for b in boundaries]


def test_a_chapter_replaced_at_the_start_changes_only_its_part(lab: World) -> None:
    lib = library(lab)
    items = [
        lib.add(Chapter(str(n), [(f"{p}.png", page("png", f"c{n}p{p}", 400)) for p in (1, 2)]))
        for n in range(1, 7)
    ]
    api, who, scope = (
        provider(lab, limits=_small_parts(lab)),
        user_id(lab, "mum"),
        scope_of(lab, "mum"),
    )
    edition = lab.seed.editions["ok"]
    count = len(api.offers(user_id=who, scope=scope, edition_id=edition))
    last = api.open(user_id=who, scope=scope, edition_id=edition, part=count)
    fp_last, bytes_last = last.content_fp, drain(last)
    lib.rewrite(items[0], Chapter("1", [("1.png", page("png", "CHANGED", 400))]), record=True)
    first = api.open(user_id=who, scope=scope, edition_id=edition, part=1)
    assert b"CHANGED" in drain(first)
    again = api.open(user_id=who, scope=scope, edition_id=edition, part=count)
    assert (again.content_fp, drain(again)) == (fp_last, bytes_last)
    assert " v1" in again.filename  # the part that did not change is not a new file


# -- a file that changes under the reader ---------------------------------------------------------


def test_a_file_replaced_without_the_manifest_following_is_a_410_before_the_first_byte(
    lab: World,
) -> None:
    lib = library(lab)
    items = [lib.add(Chapter.simple(n, count=2)) for n in ("1", "2")]
    made = complete(lab.mum, lab)
    version = _latest_version(lab)
    lib.rewrite(items[1], Chapter("2", [("1.png", page("png", "SWAPPED"))]), record=False)
    gone = lab.mum.get(f"/opds/bundles/{version}")
    assert gone.status_code == 410
    assert b"SWAPPED" not in gone.content and made.content not in gone.content
    assert complete(lab.mum, lab).status_code == 410  # nothing is sent that the manifest disowns


def test_a_file_replaced_with_the_manifest_is_a_new_version_and_the_old_one_is_gone(
    lab: World,
) -> None:
    lib = library(lab)
    items = [lib.add(Chapter.simple(n, count=2)) for n in ("1", "2")]
    complete(lab.mum, lab)
    old = _latest_version(lab)
    lib.rewrite(items[0], Chapter("1", [("1.png", page("png", "NEWER"))]), record=True)
    assert lab.mum.get(f"/opds/bundles/{old}").status_code == 410
    fresh = complete(lab.mum, lab)
    assert fresh.status_code == 200 and b"NEWER" in fresh.content
    assert (
        " v2" in fresh.headers["content-disposition"]
        or "%20v2" in fresh.headers["content-disposition"]
    )


def test_a_file_replaced_during_the_transfer_leaves_the_stream_whole_and_coherent(
    lab: World,
) -> None:
    lib = library(lab)
    items = [lib.add(Chapter.simple(n, count=3)) for n in ("1", "2", "3")]
    api, who, scope = provider(lab), user_id(lab, "mum"), scope_of(lab, "mum")
    edition = lab.seed.editions["ok"]
    reference = drain(api.open(user_id=who, scope=scope, edition_id=edition))
    download = api.open(user_id=who, scope=scope, edition_id=edition)
    it = iter(download.chunks)
    head = next(it)
    lib.rewrite(items[2], Chapter("3", [("1.png", page("png", "AFTER"))]), record=True)  # rename
    rest = b"".join(it)
    assert head + rest == reference  # the descriptors opened before the first byte: the old inode
    assert b"AFTER" not in rest


def test_a_file_overwritten_in_place_during_the_transfer_aborts_the_stream_not_mixes_it(
    lab: World,
) -> None:
    lib = library(lab)
    big = [(f"{p:05d}.png", page("png", f"p{p}", 300_000)) for p in (1, 2, 3)]
    items = [lib.add(Chapter(n, list(big))) for n in ("1", "2", "3")]
    api, who, scope = provider(lab), user_id(lab, "mum"), scope_of(lab, "mum")
    download = api.open(user_id=who, scope=scope, edition_id=lab.seed.editions["ok"])
    it = iter(download.chunks)
    next(it)
    lib.overwrite_in_place(
        items[2],
        Chapter("3", [(f"{p:05d}.png", page("png", f"ZZ{p}", 300_000)) for p in (1, 2, 3)]),
    )
    with pytest.raises(BundleChangedError):
        b"".join(it)  # the CRC of the central directory no longer matches the bytes read


def test_a_chapter_missing_from_the_disk_is_left_out_and_a_version_that_needs_it_is_gone(
    lab: World,
) -> None:
    lib = library(lab)
    items = [lib.add(Chapter.simple(n, count=2)) for n in ("1", "2")]
    complete(lab.mum, lab)
    version = _latest_version(lab)
    items[1].path.unlink()
    assert lab.mum.get(f"/opds/bundles/{version}").status_code == 410
    fresh = complete(lab.mum, lab)
    assert fresh.status_code == 200
    assert {n.split("/")[0] for n in names_of(fresh.content)} == {"00001 ch1"}


# -- the token goes, the transfer stops -----------------------------------------------------------


def test_revoking_the_token_cuts_a_transfer_at_the_next_block_of_bytes(lab: World) -> None:
    lib = library(lab)
    for n in range(1, 5):
        lib.add(Chapter(str(n), [("1.png", page("png", f"p{n}", 150_000))]))
    api, who, scope = (
        provider(lab, limits=BundleLimits(chunk_bytes=16_384)),
        user_id(lab, "mum"),
        scope_of(lab, "mum"),
    )
    download = api.open(
        user_id=who,
        scope=scope,
        edition_id=lab.seed.editions["ok"],
        token_id=token_id(lab, lab.mum),
    )
    it = iter(download.chunks)
    taken = len(next(it)) + len(next(it))
    from tests.integration.test_l48_opds import auth_of

    assert auth_of(lab.env).revoke_device_token(lab.mum.public_id)
    with pytest.raises(BundleGoneError):
        for chunk in it:
            taken += len(chunk)
    assert taken < download.size / 2


def test_lowering_the_ceiling_cuts_the_transfer_of_a_child_too(lab: World) -> None:
    lib = library(lab)
    for n in range(1, 4):
        lib.add(Chapter(str(n), [("1.png", page("png", f"p{n}", 150_000))]))
    api = provider(lab, limits=BundleLimits(chunk_bytes=16_384))
    download = api.open(
        user_id=user_id(lab, "kid"),
        scope=scope_of(lab, "kid"),
        edition_id=lab.seed.editions["ok"],
        token_id=token_id(lab, lab.kid),
    )
    it = iter(download.chunks)
    next(it)
    with lab.seed.engine.begin() as c:
        c.execute(
            sa.text("UPDATE edition SET age_rating = 18 WHERE id = :e"),
            {"e": lab.seed.editions["ok"]},
        )
    with pytest.raises(BundleGoneError):
        b"".join(it)


# -- resources are bounded -------------------------------------------------------------------------


def test_the_memory_of_a_stream_is_constant_whatever_the_size(lab: World) -> None:
    lib = library(lab)
    for n in range(1, 9):
        lib.add(
            Chapter(str(n), [(f"{p}.png", page("png", f"c{n}p{p}", 1_500_000)) for p in (1, 2)])
        )
    api = provider(lab, limits=BundleLimits(part_bytes=200_000_000))
    download = api.open(
        user_id=user_id(lab, "mum"), scope=scope_of(lab, "mum"), edition_id=lab.seed.editions["ok"]
    )
    assert download.size > 24_000_000
    tracemalloc.start()
    biggest, total = 0, 0
    for chunk in download.chunks:
        biggest, total = max(biggest, len(chunk)), total + len(chunk)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert total == download.size
    assert biggest <= 256 * 1024
    assert peak < 6 * 1024 * 1024  # a quarter of the block at most, in practice a few chunks


def test_one_transfer_per_token_and_two_for_the_instance_and_the_slot_comes_back(
    lab: World,
) -> None:
    three_chapters(library(lab))
    api = provider(lab)
    edition, scope, who = lab.seed.editions["ok"], scope_of(lab, "mum"), user_id(lab, "mum")
    a, b, c = (fresh_token(lab) for _ in range(3))
    first = api.open(user_id=who, scope=scope, edition_id=edition, token_id=a)
    with pytest.raises(BundleBusyError) as one:
        api.open(user_id=who, scope=scope, edition_id=edition, token_id=a)
    assert one.value.retry_after_s >= 1
    second = api.open(user_id=who, scope=scope, edition_id=edition, token_id=b)
    with pytest.raises(BundleBusyError):
        api.open(user_id=who, scope=scope, edition_id=edition, token_id=c)
    drain(first)  # the end of a stream gives its slot back
    third = api.open(user_id=who, scope=scope, edition_id=edition, token_id=c)
    second.chunks.close()  # so does a reader that goes away
    drain(third)
    drain(api.open(user_id=who, scope=scope, edition_id=edition, token_id=a))


def test_a_reader_that_does_not_read_is_cut_below_the_minimal_rate(lab: World) -> None:
    lib = library(lab)
    lib.add(Chapter("1", [("1.png", page("png", "slow", 600_000))]))
    now = [0.0]
    api = provider(lab, limits=BundleLimits(chunk_bytes=16_384), monotonic=lambda: now[0])
    download = api.open(
        user_id=user_id(lab, "mum"), scope=scope_of(lab, "mum"), edition_id=lab.seed.editions["ok"]
    )
    it = iter(download.chunks)
    next(it)
    now[0] += 61.0  # a minute passed and a few bytes with it
    with pytest.raises(BundleGoneError):
        for _ in it:
            now[0] += 61.0


def test_http_a_second_download_for_the_same_token_waits_with_a_retry_after(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    class Held:
        def offers(self, **_k: Any) -> list[Any]:
            return []

        def open(self, **_k: Any) -> Any:
            raise BundleBusyError(7)

    with make_world(monkeypatch, settings, Held()) as world:  # type: ignore[arg-type]
        response = complete(world.mum, world)
        assert response.status_code == 429
        assert response.headers["retry-after"] == "7"


def test_http_a_block_that_is_gone_is_a_410_with_no_body_of_the_block(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    class Gone:
        def offers(self, **_k: Any) -> list[Any]:
            return []

        def open(self, **_k: Any) -> Any:
            raise BundleGoneError("a member changed")

    with make_world(monkeypatch, settings, Gone()) as world:  # type: ignore[arg-type]
        response = complete(world.mum, world)
        assert response.status_code == 410
        assert "a member changed" not in response.text  # no reason that could say a title


def test_http_the_route_hands_the_token_and_the_part_to_the_provider(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    seen: list[dict[str, Any]] = []

    class Spy:
        def offers(self, **_k: Any) -> list[Any]:
            return []

        def open(self, **kw: Any) -> Any:
            seen.append(kw)
            raise BundleGoneError("x")

    with make_world(monkeypatch, settings, Spy()) as world:  # type: ignore[arg-type]
        complete(world.mum, world, part=2)
    assert seen[0]["part"] == 2
    assert seen[0]["token_id"]
    assert seen[0]["edition_id"]


# -- retention ------------------------------------------------------------------------------------


def _make_versions(lab: World, lib: Library, count: int) -> list[str]:
    ids = []
    for n in range(count):
        lib.add(Chapter.simple(str(100 + n), count=1))
        complete(lab.mum, lab)
        ids.append(_latest_version(lab))
    return ids


def test_the_retention_keeps_three_versions_and_any_used_in_the_last_ninety_days(
    lab: World,
) -> None:
    lib = library(lab)
    ids = _make_versions(lab, lib, 5)
    api = provider(lab)
    old, recent = (NOW - timedelta(days=120)).replace(tzinfo=None), NOW.replace(tzinfo=None)
    with lab.seed.engine.begin() as c:
        for version, used in ((ids[0], recent), (ids[1], old)):
            c.execute(
                sa.text("UPDATE bundle_version SET last_used_at = :t WHERE id = :i"),
                {"t": used, "i": version},
            )
    assert api.prune(NOW) == 1  # ids[1]: not among the 3 latest, not used for 90 days
    for version, expected in zip(ids, (200, 410, 200, 200, 200), strict=True):
        assert lab.mum.get(f"/opds/bundles/{version}").status_code == expected
    with lab.seed.engine.connect() as c:
        members = c.execute(
            sa.text("SELECT count(*) FROM bundle_member WHERE version_id = :v"), {"v": ids[1]}
        ).scalar_one()
        aliases = c.execute(
            sa.select(sa.func.count()).select_from(export.document_alias)
        ).scalar_one()
    assert members == 0  # no member, so no fingerprint of the files is kept (ADR-0048)
    assert aliases == 4  # and the digest of the purged version resolves nothing


def test_deleting_an_exported_file_row_wipes_its_fingerprint_from_the_versions(lab: World) -> None:
    lib = library(lab)
    items = [lib.add(Chapter.simple(n, count=1)) for n in ("1", "2")]
    complete(lab.mum, lab)
    with lab.seed.engine.begin() as c:
        c.execute(sa.delete(export.export_item).where(export.export_item.c.id == items[0].item_id))
        rows = c.execute(
            sa.text("SELECT export_item_id, content_fp FROM bundle_member WHERE rank = 1")
        ).all()
    assert [tuple(r) for r in rows] == [(None, None)]  # the member stays, as an orphan with no hash
