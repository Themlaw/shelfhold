"""M4 L47: the kosync routes of Shelfhold, the other half of the e-reader's circuit
(ADR-0053 §5 and §6, EF-LEC-009, EF-LEC-014, EF-USR-006, EF-USR-008, ENF-SEC-010).

The shapes are the ones KOReader and the reference sync server speak (`plugins/kosync.koplugin/`
`api.json`, `KOSyncClient.lua`, `main.lua`; `koreader/koreader-sync-server`
`app/controllers/1/syncs_controller.lua`, `config/errors.lua`): `PUT` answers 200 with
`{document, timestamp}`, `GET` answers 200 with `{document, progress, percentage, device,
device_id, timestamp}` (`progress` in text, `timestamp` in whole seconds), and 200 with `{}` when
nothing is known; `POST /users/create` answers 402.

The attacks go through the real application: a child's token, a member's, a revoked one, a session,
an API key, another profile's token and fingerprints (IDOR), a page that is negative, huge or not
a number, a body that is far too big, an xpointer that is a trap, and a burst.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import export, reading
from shelfhold.reading.bundle import BundleLimits, SeriesBundles
from shelfhold.reading.domain.document_hash import koreader_partial_md5
from tests.bundle_env import Chapter, Exported, Library, library_for, page
from tests.integration.test_l46_device_tokens import kosync
from tests.integration.test_l48_opds import Reader, World, auth_of, make_world

pytestmark = pytest.mark.req("EF-LEC-009", "EF-LEC-014", "EF-USR-006", "EF-USR-008", "ENF-SEC-010")

PUT = "/kosync/syncs/progress"
ABSENT_MD5 = "0123456789abcdef0123456789abcdef"
XPOINTER = "/body/DocFragment[3]/body/div/p[12]/text().5"


@pytest.fixture
def lab(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with make_world(monkeypatch, settings) as made:
        yield made


@pytest.fixture
def parted(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    """The application with a block cut in small parts (the cap of L49a, lowered)."""
    monkeypatch.setattr(
        "shelfhold.main.series_bundles",
        lambda engine, keyring, now: SeriesBundles(
            engine, keyring, now=now, limits=BundleLimits(part_bytes=3_000)
        ),
    )
    with make_world(monkeypatch, settings) as made:
        yield made


# -- the library and the calls ---------------------------------------------------------------


def library(world: World, key: str = "ok") -> Library:
    root = world.env.settings.data_dir.parent / f"lib-{key}"
    return library_for(world.seed.engine, root, world.seed.editions[key])


def md5_of(body: bytes) -> str:
    return koreader_partial_md5(io.BytesIO(body))


def alias(world: World, item: Exported) -> str:
    """The digest KOReader would send for the file: the alias the exporter leaves (L13)."""
    digest = md5_of(item.path.read_bytes())
    with world.seed.engine.begin() as c:
        c.execute(
            sa.insert(export.document_alias).values(
                export_item_id=item.item_id,
                file_hash="sha256:" + "ab" * 32,
                content_hash="sha256:" + "ab" * 32,
                koreader_partial_md5="md5:" + digest,
            )
        )
    return digest


def volume(
    world: World, key: str = "ok", count: int = 20, fmt: str = "cbz"
) -> tuple[Exported, str]:
    """One exported file with its digest."""
    chapter = Chapter.simple("1", count=count, fmt=fmt)
    item = library(world, key).add(chapter)
    return item, alias(world, item)


def push(reader: Reader, document: str, progress: Any, percentage: Any = 0.5, **extra: Any) -> Any:
    body = {
        "document": document, "progress": progress, "percentage": percentage,
        "device": "Libra 2", "device_id": "A1B2C3", **extra,
    }  # fmt: skip
    return reader.client.put(PUT, json=body, headers=kosync(reader.public_id, reader.secret))


def pull(reader: Reader, document: str) -> Any:
    return reader.client.get(
        f"/kosync/syncs/progress/{document}", headers=kosync(reader.public_id, reader.secret)
    )


def positions(world: World) -> list[Any]:
    with world.seed.engine.connect() as c:
        return list(
            c.execute(sa.select(reading.reading_position).order_by(reading.reading_position.c.id))
        )


def block(world: World, reader: Reader, key: str = "ok") -> tuple[str, bytes]:
    """The block of an Edition as the reader downloads it, and the digest KOReader computes."""
    answer = reader.get(f"/opds/editions/{world.seed.editions[key]}/complete")
    assert answer.status_code == 200, answer.text
    return md5_of(answer.content), answer.content


def three_chapters(world: World, key: str = "ok") -> list[Exported]:
    lib = library(world, key)
    return [lib.add(Chapter.simple(n, count=3)) for n in ("1", "2", "10")]


def shape(answer: Any) -> tuple[int, Any, dict[str, str]]:
    """What a client can tell two answers apart by (no date, no request id)."""
    body = answer.json()
    if isinstance(body, dict):
        body = {k: v for k, v in body.items() if k != "request_id"}
    kept = {
        k: v for k, v in answer.headers.items()
        if k.lower() in {"content-type", "cache-control", "www-authenticate"}
    }  # fmt: skip
    return answer.status_code, body, kept


class Statements:
    """The statements the application ran on the tables kosync reads, in order: two answers that
    must cost the same time run the same ones."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def _on(self, _c: Any, _cur: Any, statement: str, *_a: Any) -> None:
        if re.search(
            r"document_alias|bundle_version|bundle_member|reading_position|export_item", statement
        ):
            self.seen.append(re.sub(r"\s+", " ", statement))

    def __enter__(self) -> Statements:
        event.listen(Engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *_a: object) -> None:
        event.remove(Engine, "before_cursor_execute", self._on)


# -- the protocol: shapes and codes KOReader waits for -----------------------------------------


def test_put_then_get_speak_the_shapes_of_the_reference_server(lab: World) -> None:
    _item, digest = volume(lab)
    done = push(lab.mum, digest, "7", 0.35)
    assert done.status_code == 200
    assert set(done.json()) == {"document", "timestamp"}
    assert done.json()["document"] == digest and isinstance(done.json()["timestamp"], int)
    got = pull(lab.mum, digest)
    assert got.status_code == 200
    body = got.json()
    assert set(body) == {"document", "progress", "percentage", "device", "device_id", "timestamp"}
    assert body["progress"] == "7" and isinstance(body["progress"], str)  # `tonumber(progress)`
    assert body["percentage"] == pytest.approx(0.35)
    assert (body["device"], body["device_id"], body["document"]) == ("Libra 2", "A1B2C3", digest)
    assert isinstance(body["timestamp"], int)
    assert got.headers["content-type"].startswith("application/json")
    assert "no-store" in got.headers["cache-control"]


def test_get_of_nothing_known_is_200_and_an_empty_object(lab: World) -> None:
    _item, digest = volume(lab)
    got = pull(lab.mum, digest)  # a known file, no position yet: KOReader says "no progress"
    assert (got.status_code, got.json()) == (200, {})


def test_users_create_is_refused_with_402_whatever_the_body_and_creates_nothing(lab: World) -> None:
    before = lab.env.settings
    for headers in ({}, kosync(lab.mum.public_id, lab.mum.secret)):
        for payload in (
            {"username": "newbie", "password": "0" * 32},
            {},
            {"username": "x" * 10**5},
        ):
            refused = lab.mum.client.post("/kosync/users/create", json=payload, headers=headers)
            assert refused.status_code == 402
            assert "newbie" not in refused.text
    with lab.seed.engine.connect() as c:
        assert (
            c.execute(sa.text("SELECT count(*) FROM user WHERE username = 'newbie'")).scalar_one()
            == 0
        )
    assert before is lab.env.settings


def test_the_methods_and_paths_outside_the_four_routes_do_not_exist(lab: World) -> None:
    headers = kosync(lab.mum.public_id, lab.mum.secret)
    assert lab.mum.client.delete("/kosync/users/me", headers=headers).status_code in (404, 405)
    assert lab.mum.client.put("/kosync/users/password", headers=headers).status_code in (404, 405)
    assert lab.mum.client.post(PUT, json={}, headers=headers).status_code == 405


# -- 🔒 1: the page arrives in text ---------------------------------------------------------------


def test_the_page_in_text_is_stored_as_an_integer(lab: World) -> None:
    _item, digest = volume(lab)
    assert push(lab.mum, digest, "12", 0.6).status_code == 200
    (row,) = positions(lab)
    assert row.locator["page"] == 12 and isinstance(row.locator["page"], int)
    assert row.source == "device" and row.device_id == "A1B2C3"
    with lab.seed.engine.connect() as c:
        stored = c.execute(
            sa.text("SELECT json_type(locator, '$.page') FROM reading_position")
        ).scalar_one()
    assert stored == "integer"


@pytest.mark.parametrize(
    "bad",
    ["-3", "0", "abc", "1e3", "12.5", "999999999999", "", " ", "٣", "12\x00", "1 2", "+4", "0x10"],
)
def test_a_page_that_is_not_a_whole_positive_number_is_a_422_and_stores_nothing(
    lab: World, bad: str
) -> None:
    _item, digest = volume(lab)
    answer = push(lab.mum, digest, bad)
    assert answer.status_code == 422, bad
    assert positions(lab) == []
    if len(bad.strip()) > 3:
        assert bad not in answer.text  # nothing of the request is echoed


@pytest.mark.parametrize("bad", [None, True, [1], {"page": 1}])
def test_a_progress_that_is_not_text_or_a_number_is_a_422(lab: World, bad: Any) -> None:
    _item, digest = volume(lab)
    assert push(lab.mum, digest, bad).status_code == 422
    assert positions(lab) == []


@pytest.mark.parametrize("bad", [-0.1, 1.5, "abc", None, True, [0.5]])
def test_a_percentage_outside_zero_one_is_a_422(lab: World, bad: Any) -> None:
    _item, digest = volume(lab)
    assert push(lab.mum, digest, "3", bad).status_code == 422
    assert positions(lab) == []


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_a_percentage_that_is_not_a_finite_number_is_a_422(lab: World, number: str) -> None:
    _item, digest = volume(lab)
    raw = '{"document": "%s", "progress": "3", "percentage": %s, "device": "d", "device_id": "i"}'
    answer = lab.mum.client.put(
        PUT,
        content=raw % (digest, number),
        headers={**kosync(lab.mum.public_id, lab.mum.secret), "content-type": "application/json"},
    )
    assert answer.status_code == 422 and positions(lab) == []


@pytest.mark.parametrize("missing", ["document", "progress", "percentage", "device"])
def test_a_missing_required_field_is_a_422_and_device_id_alone_is_optional(
    lab: World, missing: str
) -> None:
    _item, digest = volume(lab)
    body = {"document": digest, "progress": "3", "percentage": 0.1, "device": "d", "device_id": "i"}
    del body[missing]
    answer = lab.mum.client.put(PUT, json=body, headers=kosync(lab.mum.public_id, lab.mum.secret))
    assert answer.status_code == 422
    body = {"document": digest, "progress": "3", "percentage": 0.1, "device": "d"}
    ok = lab.mum.client.put(PUT, json=body, headers=kosync(lab.mum.public_id, lab.mum.secret))
    assert ok.status_code == 200


def test_a_body_that_is_far_too_big_is_refused_before_it_is_read_and_stores_nothing(
    lab: World,
) -> None:
    _item, digest = volume(lab)
    answer = lab.mum.client.put(
        PUT,
        content=json.dumps(
            {"document": digest, "progress": "3", "percentage": 0.1, "device": "d" * 10**6}
        ),
        headers={**kosync(lab.mum.public_id, lab.mum.secret), "content-type": "application/json"},
    )
    assert answer.status_code == 413
    assert positions(lab) == []


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", '"text"', "null", "{"])
def test_a_body_that_is_not_an_object_is_a_422(lab: World, raw: str) -> None:
    answer = lab.mum.client.put(
        PUT,
        content=raw,
        headers={**kosync(lab.mum.public_id, lab.mum.secret), "content-type": "application/json"},
    )
    assert answer.status_code == 422


def test_a_field_too_long_is_a_422(lab: World) -> None:
    _item, digest = volume(lab)
    assert push(lab.mum, digest, "3", device="d" * 500).status_code == 422
    assert push(lab.mum, digest, "3", device_id="i" * 500).status_code == 422
    assert positions(lab) == []


# -- 🔒 1: the xpointer of an EPUB is stored as it is and marks nothing ----------------------


def test_an_epub_xpointer_is_stored_as_it_is_and_given_back(lab: World) -> None:
    _item, digest = volume(lab, count=1, fmt="epub")
    assert push(lab.mum, digest, XPOINTER, 0.3).status_code == 200
    (row,) = positions(lab)
    assert row.locator.get("xpointer") == XPOINTER and "page" not in row.locator
    assert pull(lab.mum, digest).json()["progress"] == XPOINTER


def test_a_walk_through_an_epub_marks_nothing_read_until_l51(lab: World) -> None:
    item, digest = volume(lab, count=1, fmt="epub")
    for step in range(1, 26):  # 4 % at a time: under the 5 % a share walk admits
        assert push(lab.mum, digest, f"{XPOINTER}{step}", step * 0.04).status_code == 200
    with lab.seed.engine.connect() as c:
        states = (
            c.execute(
                sa.text("SELECT state FROM reading_progress WHERE unit_id = :u"),
                {"u": item.unit_id},
            )
            .scalars()
            .all()
        )
        marks = c.execute(
            sa.text("SELECT count(*) FROM reading_position WHERE json_array_length(read_units) > 0")
        ).scalar_one()
    assert "read" not in states and marks == 0


@pytest.mark.parametrize(
    "trap",
    [
        "/body\x00/p",
        "/body\n/p",
        "/body\r\nSet-Cookie: x=1",
        "\x1b[31m",
        "a" * 5000,
        "<script>alert(1)</script>",
        "'; DROP TABLE reading_position; --",
        "‮/body",
        "",
    ],
)
def test_a_trapped_xpointer_is_refused_or_kept_inert(lab: World, trap: str) -> None:
    _item, digest = volume(lab, count=1, fmt="epub")
    answer = push(lab.mum, digest, trap)
    rows = positions(lab)
    if answer.status_code == 200:  # kept: as a string, never as a page, never executed
        assert [r.locator for r in rows] == [{"xpointer": trap}] or all(
            "page" not in r.locator for r in rows
        )
        got = pull(lab.mum, digest)
        assert got.status_code == 200 and got.json()["progress"] == trap
        assert got.headers["content-type"].startswith("application/json")
        assert "set-cookie" not in got.headers
    else:
        assert answer.status_code == 422 and rows == []
    with lab.seed.engine.connect() as c:  # the table is still there
        c.execute(sa.text("SELECT 1 FROM reading_position LIMIT 1"))


def test_a_json_looking_xpointer_cannot_forge_a_page(lab: World) -> None:
    _item, digest = volume(lab, count=1, fmt="epub")
    push(lab.mum, digest, '{"page": 99999}', 0.2)
    assert all("page" not in r.locator for r in positions(lab))


def test_a_page_less_text_for_a_paged_file_is_a_422_not_an_xpointer(lab: World) -> None:
    _item, digest = volume(lab, count=5)
    assert push(lab.mum, digest, XPOINTER).status_code == 422
    assert positions(lab) == []


# -- 🔒 2: an unknown document and a hidden one answer the same -----------------------------


def test_an_unknown_document_is_202_on_put_and_an_empty_200_on_get_and_stores_nothing(
    lab: World,
) -> None:
    put_answer = push(lab.mum, ABSENT_MD5, "5")
    assert (put_answer.status_code, put_answer.json()) == (202, {})
    assert (pull(lab.mum, ABSENT_MD5).status_code, pull(lab.mum, ABSENT_MD5).json()) == (200, {})
    assert positions(lab) == []


@pytest.mark.parametrize(
    "document", ["", "zz", "A" * 33, "../../etc/passwd", "%00", "0" * 200, "é" * 32]
)
def test_a_document_that_is_not_a_digest_answers_like_an_unknown_one(
    lab: World, document: str
) -> None:
    unknown = push(lab.mum, ABSENT_MD5, "5")
    odd = push(lab.mum, document, "5")
    assert odd.status_code in (202, 422) and (
        odd.status_code != 202 or shape(odd) == shape(unknown)
    )
    if document and "/" not in document and "%" not in document:
        assert pull(lab.mum, document).status_code in (200, 404, 422)
    assert positions(lab) == []


def test_a_document_above_the_ceiling_answers_exactly_like_an_unknown_one(lab: World) -> None:
    _hidden, digest = volume(lab, key="above")  # the adult edition: a child may not see it
    with Statements() as unknown_cost:
        unknown_put, unknown_get = push(lab.kid, ABSENT_MD5, "5"), pull(lab.kid, ABSENT_MD5)
    with Statements() as hidden_cost:
        hidden_put, hidden_get = push(lab.kid, digest, "5"), pull(lab.kid, digest)
    assert shape(hidden_put) == shape(unknown_put) == (202, {}, shape(unknown_put)[2])
    assert shape(hidden_get) == shape(unknown_get)
    assert hidden_cost.seen == unknown_cost.seen  # the same work: no clock tells them apart
    assert positions(lab) == []
    # the control: an adult holds it, and it is not the child's answer that lies
    assert push(lab.mum, digest, "5").status_code == 200


def test_a_digest_of_another_profiles_block_is_an_unknown_one_for_this_profile(lab: World) -> None:
    three_chapters(lab)
    mine, _ = block(lab, lab.mum)
    assert push(lab.mum, mine, "2", 2 / 9).status_code == 200  # the control
    with Statements() as own_unknown:
        base = push(lab.kid, ABSENT_MD5, "2", 2 / 9)
    with Statements() as stolen:
        theirs = push(lab.kid, mine, "2", 2 / 9)  # the child never downloaded that version
    assert shape(theirs) == shape(base) and stolen.seen == own_unknown.seen
    assert shape(pull(lab.kid, mine)) == shape(pull(lab.kid, ABSENT_MD5))
    with lab.seed.engine.connect() as c:
        kid_rows = c.execute(
            sa.text(
                "SELECT count(*) FROM reading_position p JOIN user u ON u.id = p.user_id "
                "WHERE u.username = 'kid'"
            )
        ).scalar_one()
    assert kid_rows == 0


def test_a_digest_held_by_two_visible_files_is_a_409_not_a_guess(lab: World) -> None:
    first, digest = volume(lab)
    other = library(lab, "ok").add(Chapter.simple("2", count=20))
    with lab.seed.engine.begin() as c:
        c.execute(
            sa.insert(export.document_alias).values(
                export_item_id=other.item_id,
                file_hash="sha256:" + "cd" * 32,
                content_hash="sha256:" + "cd" * 32,
                koreader_partial_md5="md5:" + digest,
            )
        )
    assert first.item_id != other.item_id
    assert push(lab.mum, digest, "3").status_code == 409
    assert pull(lab.mum, digest).status_code == 409
    assert positions(lab) == []


def test_a_hidden_twin_of_a_digest_is_never_a_409_for_a_child(lab: World) -> None:
    """The adult edition that shares a digest with a visible file must not turn the child's
    answer into a conflict: it would tell her the other file exists."""
    _visible, digest = volume(lab)
    hidden = library(lab, "above").add(Chapter.simple("2", count=20))
    with lab.seed.engine.begin() as c:
        c.execute(
            sa.insert(export.document_alias).values(
                export_item_id=hidden.item_id,
                file_hash="sha256:" + "cd" * 32,
                content_hash="sha256:" + "cd" * 32,
                koreader_partial_md5="md5:" + digest,
            )
        )
    assert push(lab.kid, digest, "3").status_code == 200


def test_the_unknown_digests_are_counted_by_token_without_naming_what_was_asked(lab: World) -> None:
    for _ in range(3):
        push(lab.mum, ABSENT_MD5, "5")
    counts = lab.env.app.state.kosync_unknown
    assert counts.of(lab.mum.public_id) == 3 and counts.of(lab.kid.public_id) == 0
    assert ABSENT_MD5 not in json.dumps(counts.snapshot())


# -- 🔒 3: the block of a series: the page of the part becomes a place in the Work ----------------


def test_a_page_of_the_block_is_the_chapter_and_page_of_the_work(lab: World) -> None:
    items = three_chapters(lab)  # 3 pages each: the block has 9
    digest, _ = block(lab, lab.mum)
    assert push(lab.mum, digest, "5", 5 / 9).status_code == 200  # 2nd chapter, 2nd page
    (row,) = positions(lab)
    assert (row.carrier_type, row.carrier_id) == ("export_item", items[1].item_id)
    assert row.locator["page"] == 2 and row.percent == pytest.approx(2 / 3)
    assert row.device_id == "A1B2C3"
    with lab.seed.engine.connect() as c:
        reached = c.execute(
            sa.text("SELECT unit_id, state FROM reading_progress WHERE user_id = :u"),
            {"u": row.user_id},
        ).all()
    assert [(r.unit_id, r.state) for r in reached] == [
        (items[1].unit_id, "in_progress")
    ]  # only that one


def test_the_first_and_the_last_page_of_a_part_are_the_first_and_last_chapters(lab: World) -> None:
    items = three_chapters(lab)
    digest, _ = block(lab, lab.mum)
    assert push(lab.mum, digest, "1", 1 / 9).status_code == 200
    assert push(lab.mum, digest, "9", 1.0).status_code == 200
    first, last = positions(lab)
    assert (first.carrier_id, first.locator["page"]) == (items[0].item_id, 1)
    assert (last.carrier_id, last.locator["page"]) == (items[2].item_id, 3)


def test_the_page_after_the_last_and_the_page_zero_of_a_block_are_a_422(lab: World) -> None:
    three_chapters(lab)
    digest, _ = block(lab, lab.mum)
    assert push(lab.mum, digest, "10", 1.0).status_code == 422
    assert push(lab.mum, digest, "0", 0.0).status_code == 422
    assert positions(lab) == []


def _two_parts(world: World) -> tuple[list[Exported], list[str]]:
    lib = library(world)
    items = [
        lib.add(Chapter(str(n), [(f"{p}.png", page("png", f"c{n}p{p}", 400)) for p in (1, 2)]))
        for n in range(1, 7)
    ]
    digests = []
    for part in (1, 2):
        answer = world.mum.get(f"/opds/editions/{world.seed.editions['ok']}/complete?part={part}")
        assert answer.status_code == 200
        digests.append(md5_of(answer.content))
    return items, digests


def test_the_parts_one_and_n_translate_from_their_own_first_page(parted: World) -> None:
    items, (part_one, part_last) = _two_parts(parted)
    with parted.seed.engine.connect() as c:
        parts = c.execute(
            sa.text(
                "SELECT part_index, page_count, member_count FROM bundle_version "
                "ORDER BY part_index"
            )
        ).all()
    assert len(parts) >= 2
    assert push(parted.mum, part_one, "1", 1 / parts[0].page_count).status_code == 200
    assert push(parted.mum, part_last, "2", 2 / parts[-1].page_count).status_code == 200
    first, last = positions(parted)
    assert (first.carrier_id, first.locator["page"]) == (items[0].item_id, 1)  # part 1, page 1
    with parted.seed.engine.connect() as c:  # the chapter that opens the last part
        opening = c.execute(
            sa.text(
                "SELECT m.export_item_id FROM bundle_member m "
                "JOIN bundle_version v ON v.id = m.version_id "
                "WHERE v.part_index = :p AND m.rank = 1"
            ),
            {"p": parts[-1].part_index},
        ).scalar_one()
    assert opening in {i.item_id for i in items} and opening != items[0].item_id
    assert (last.carrier_id, last.locator["page"]) == (opening, 2)  # page 2 of the last part


def test_the_same_page_number_in_two_parts_is_two_different_chapters(parted: World) -> None:
    _items, (one, last) = _two_parts(parted)
    assert one != last
    push(parted.mum, one, "1", 0.2)
    push(parted.mum, last, "1", 0.2)
    a, b = positions(parted)
    assert a.carrier_id != b.carrier_id


def test_a_purged_version_is_an_unknown_document_and_stores_nothing(lab: World) -> None:
    three_chapters(lab)
    digest, _ = block(lab, lab.mum)
    assert push(lab.mum, digest, "4").status_code == 200
    with lab.seed.engine.begin() as c:
        c.execute(sa.text("UPDATE bundle_version SET purged_at = CURRENT_TIMESTAMP"))
        c.execute(sa.text("DELETE FROM bundle_member"))
        c.execute(
            sa.delete(export.document_alias).where(
                export.document_alias.c.koreader_partial_md5 == "md5:" + digest
            )
        )
    before = len(positions(lab))
    after = push(lab.mum, digest, "5")
    assert shape(after) == shape(push(lab.mum, ABSENT_MD5, "5")) and len(positions(lab)) == before
    assert pull(lab.mum, digest).json() == {}


def test_a_chapter_purged_from_a_version_is_an_orphan_position_that_is_not_stored(
    lab: World,
) -> None:
    items = three_chapters(lab)
    digest, _ = block(lab, lab.mum)
    with lab.seed.engine.begin() as c:
        c.execute(sa.delete(export.export_item).where(export.export_item.c.id == items[1].item_id))
    answer = push(lab.mum, digest, "5", 5 / 9)  # the page that was in the purged chapter
    assert answer.status_code == 202 and positions(lab) == []
    assert push(lab.mum, digest, "1", 1 / 9).status_code == 200  # the others still answer


def test_a_chapter_replaced_since_the_version_is_never_given_a_page_of_the_old_cut(
    lab: World,
) -> None:
    items = three_chapters(lab)
    digest, _ = block(lab, lab.mum)
    lab_lib = library(lab)
    lab_lib.items = items
    lab_lib.rewrite(items[1], Chapter("2", [("1.png", page("png", "NEWER"))]), record=True)
    answer = push(lab.mum, digest, "5", 5 / 9)
    assert answer.status_code == 202 and positions(lab) == []  # the layout it speaks of is gone


def test_the_guard_of_integrity_refuses_a_percentage_that_is_not_of_this_file(lab: World) -> None:
    three_chapters(lab)
    digest, _ = block(lab, lab.mum)
    # page 5 of a file that has 9 pages is at 55.6 %: 5 / 0.1 = 50 pages is another file
    answer = push(lab.mum, digest, "5", 0.1)
    assert answer.status_code == 202 and positions(lab) == []
    assert push(lab.mum, digest, "5", 0.5556).status_code == 200  # KOReader rounds to 1/10000
    assert push(lab.mum, digest, "5", 0.5).status_code == 200  # within max(2, 0.5 %) pages: 10


# -- 🔒 4: what GET gives back is in the frame of the document asked -------------------------


def test_the_get_gives_the_furthest_position_with_its_own_date_never_now(lab: World) -> None:
    """The rule that is fixed by ADR-0053 §5: read chapter 10, go back to chapter 1, close: the
    reader that opens the file again is told chapter 10, dated when it was read."""
    three_chapters(lab)
    digest, _ = block(lab, lab.mum)
    far = push(lab.mum, digest, "8", 8 / 9, device="Libra 2", device_id="A1B2C3")
    back = push(lab.mum, digest, "1", 1 / 9, device="Libra 2", device_id="A1B2C3")
    assert far.status_code == back.status_code == 200
    with lab.seed.engine.begin() as c:  # the furthest was written long ago
        c.execute(
            sa.text(
                "UPDATE reading_position SET server_ts = :t "
                "WHERE json_extract(locator, '$.page') = 2"
            ),
            {"t": (positions(lab)[0].server_ts - timedelta(days=2)).replace(tzinfo=None)},
        )
    got = pull(lab.mum, digest).json()
    assert got["progress"] == "8" and got["percentage"] == pytest.approx(8 / 9)
    assert got["timestamp"] == int((positions(lab)[0].server_ts).timestamp())
    assert got["timestamp"] < int(positions(lab)[1].server_ts.timestamp())


def test_a_position_of_a_chapter_file_is_given_back_in_the_page_of_the_block(lab: World) -> None:
    """Read chapter 10 page 2 elsewhere (the file of the chapter itself, or Kavita): the block
    opens at page 8 of 9."""
    items = three_chapters(lab)
    digest, _ = block(lab, lab.mum)
    chapter_digest = alias(lab, items[2])
    assert (
        push(lab.mum, chapter_digest, "2", 2 / 3, device="Phone", device_id="P1").status_code == 200
    )
    got = pull(lab.mum, digest)
    assert got.status_code == 200
    body = got.json()
    assert body["progress"] == "8" and body["percentage"] == pytest.approx(8 / 9)
    assert (body["device"], body["device_id"]) == ("Phone", "P1")  # the source of the position
    assert body["document"] == digest


def test_a_position_of_the_block_is_given_back_in_the_page_of_the_chapter_file(lab: World) -> None:
    items = three_chapters(lab)
    digest, _ = block(lab, lab.mum)
    chapter_digest = alias(lab, items[1])
    push(lab.mum, digest, "5", 5 / 9)  # chapter 2, page 2
    got = pull(lab.mum, chapter_digest).json()
    assert got["progress"] == "2" and got["percentage"] == pytest.approx(2 / 3)


def test_a_position_on_another_file_of_the_work_that_cannot_be_translated_is_not_given(
    lab: World,
) -> None:
    """Two files of one Unit: pages of one say nothing of the pages of the other."""
    first, digest_a = volume(lab, count=30)
    lib = library(lab)
    second = lib.add(Chapter.simple("9", count=12))
    digest_b = alias(lab, second)
    with lab.seed.engine.begin() as c:  # the second file covers the same Unit as the first
        c.execute(
            sa.update(export.unit_coverage)
            .where(export.unit_coverage.c.carrier_id == second.item_id)
            .values(unit_id=first.unit_id)
        )
    assert push(lab.mum, digest_a, "20", 20 / 30).status_code == 200
    assert pull(lab.mum, digest_b).json() == {}  # not "page 20 of 12"
    assert pull(lab.mum, digest_a).json()["progress"] == "20"


def test_a_server_state_position_is_given_as_kavita_with_a_stable_device_id(lab: World) -> None:
    items = three_chapters(lab)
    digest, _ = block(lab, lab.mum)
    uid = positions_user(lab, "mum")
    with lab.seed.engine.begin() as c:
        anchor = c.execute(
            sa.select(export.export_record.c.file_hash).where(
                export.export_record.c.export_item_id == items[2].item_id
            )
        ).scalar_one()
        c.execute(
            sa.insert(reading.reading_position).values(
                id="01HZZZZZZZZZZZZZZZZZZZZZZA",
                user_id=uid,
                carrier_type="export_item",
                carrier_id=items[2].item_id,
                content_hash=anchor,
                locator={"page": 2},
                percent=2 / 3,
                client_ts=sa.func.current_timestamp(),
                source="server",
                device_id="server:dest",
                covered_units=[items[2].unit_id],
            )
        )
    body = pull(lab.mum, digest).json()
    assert (
        body["progress"] == "8"
        and body["device"] == "Kavita"
        and body["device_id"] == "server:dest"
    )


def positions_user(world: World, name: str) -> str:
    with world.seed.engine.connect() as c:
        return str(
            c.execute(sa.text("SELECT id FROM user WHERE username = :n"), {"n": name}).scalar_one()
        )


def test_the_timestamp_is_never_in_the_future(lab: World) -> None:
    _item, digest = volume(lab)
    push(lab.mum, digest, "4")
    with lab.seed.engine.begin() as c:
        c.execute(sa.text("UPDATE reading_position SET server_ts = '2099-01-01 00:00:00'"))
    import time

    assert pull(lab.mum, digest).json()["timestamp"] <= int(time.time()) + 1


# -- 🔒 5: speed counts server_ts and nothing the device says ---------------------------------


def test_the_speed_rule_reads_the_server_clock_and_not_a_date_sent_by_the_device(
    lab: World,
) -> None:
    item, digest = volume(lab, count=40)
    for forged in (
        {"timestamp": 1},
        {"client_ts": "2000-01-01T00:00:00Z"},
        {"server_ts": "2000-01-01T00:00:00Z"},
    ):
        assert push(lab.mum, digest, "30", 0.75, **forged).status_code in (200, 422)
    # a leap from page 1 to page 40 in the same second is not a plausible read
    assert push(lab.mum, digest, "1", 0.025).status_code == 200
    assert push(lab.mum, digest, "40", 1.0, timestamp=10**12).status_code == 200
    with lab.seed.engine.connect() as c:
        marks = c.execute(
            sa.text("SELECT count(*) FROM reading_position WHERE json_array_length(read_units) > 0")
        ).scalar_one()
        states = (
            c.execute(
                sa.text("SELECT state FROM reading_progress WHERE unit_id = :u"),
                {"u": item.unit_id},
            )
            .scalars()
            .all()
        )
    assert marks == 0 and "read" not in states
    stamps = {r.server_ts for r in positions(lab)}
    assert all(s.year >= 2026 for s in stamps)  # the server's own clock, every time


# -- 🔒 6: the credential, read again at each request, and the limits of L46 ----------------------


def test_every_route_refuses_no_token_a_wrong_key_a_session_and_an_api_key(lab: World) -> None:
    _item, digest = volume(lab)
    key = auth_of(lab.env).create_api_key(name="k", scope="admin").value
    other = lab.env.other_client("10.0.0.77")
    for client, headers in (
        (other, {}),
        (other, kosync(lab.mum.public_id, "x" * 26)),
        (lab.kid_session, {}),
        (other, {"Authorization": f"Bearer {key}"}),
    ):
        body = {"document": digest, "progress": "1", "percentage": 0.1, "device": "d"}
        # a session is refused at the door (401), or by the CSRF check of its cookie (403)
        assert client.put(PUT, json=body, headers=headers).status_code in (401, 403)
        assert client.get(f"/kosync/syncs/progress/{digest}", headers=headers).status_code == 401
    assert positions(lab) == []


def test_a_revoked_token_is_refused_at_the_next_request_on_both_routes(lab: World) -> None:
    _item, digest = volume(lab)
    assert (
        push(lab.mum, digest, "3").status_code == 200 and pull(lab.mum, digest).status_code == 200
    )
    assert auth_of(lab.env).revoke_device_token(lab.mum.public_id) is True
    assert push(lab.mum, digest, "9").status_code == 401
    assert pull(lab.mum, digest).status_code == 401
    assert [r.locator["page"] for r in positions(lab)] == [3]


def test_a_token_that_expired_is_refused(lab: World) -> None:
    _item, digest = volume(lab)
    with lab.seed.engine.begin() as c:
        c.execute(
            sa.text(
                "UPDATE device_token SET expires_at = '2000-01-01 00:00:00' WHERE public_id = :p"
            ),
            {"p": lab.mum.public_id.lower()},
        )
    assert push(lab.mum, digest, "3").status_code == 401


def test_another_profiles_identifier_with_my_key_opens_nothing_and_writes_nothing(
    lab: World,
) -> None:
    """`x-auth-user` names a token; with the key of another one it is nobody."""
    _item, digest = volume(lab)
    headers = kosync(lab.mum.public_id, lab.kid.secret)
    answer = lab.kid.client.put(
        PUT,
        json={"document": digest, "progress": "3", "percentage": 0.1, "device": "d"},
        headers=headers,
    )
    assert answer.status_code == 401 and positions(lab) == []
    assert (
        lab.kid.client.get(f"/kosync/syncs/progress/{digest}", headers=headers).status_code == 401
    )


def test_a_body_naming_another_profile_writes_for_the_token_alone(lab: World) -> None:
    _item, digest = volume(lab)
    mum_id = positions_user(lab, "mum")
    push(lab.kid, digest, "3", user_id=mum_id, user="mum", username="mum", profile=mum_id)
    assert {r.user_id for r in positions(lab)} == {positions_user(lab, "kid")}
    assert pull(lab.mum, digest).json() == {}  # nothing of the child's reaches the adult's view


def test_two_profiles_reading_one_file_do_not_see_each_others_place(lab: World) -> None:
    _item, digest = volume(lab)
    push(lab.mum, digest, "17", 17 / 20, device="Mum's reader", device_id="M")
    push(lab.kid, digest, "2", 2 / 20, device="Kid's reader", device_id="K")
    assert pull(lab.mum, digest).json()["progress"] == "17"
    assert pull(lab.kid, digest).json()["progress"] == "2"


def test_a_child_cannot_write_or_read_a_position_on_a_work_above_her_ceiling(lab: World) -> None:
    hidden, digest = volume(lab, key="above")
    assert push(lab.mum, digest, "3").status_code == 200  # an adult's own place on it
    assert pull(lab.kid, digest).json() == {}
    assert push(lab.kid, digest, "9").status_code == 202
    with lab.seed.engine.connect() as c:
        kid_rows = c.execute(
            sa.text("SELECT count(*) FROM reading_position WHERE user_id = :u"),
            {"u": positions_user(lab, "kid")},
        ).scalar_one()
    assert kid_rows == 0 and hidden.item_id


def test_a_burst_of_wrong_keys_is_a_429_with_retry_after_and_the_right_key_waits(
    lab: World,
) -> None:
    _item, digest = volume(lab)
    statuses = [
        lab.mum.client.get(
            f"/kosync/syncs/progress/{digest}", headers=kosync(lab.mum.public_id, "w" * 26)
        ).status_code
        for _ in range(12)
    ]
    assert statuses[:5] == [401] * 5 and 429 in statuses
    limited = lab.mum.client.get(
        f"/kosync/syncs/progress/{digest}", headers=kosync(lab.mum.public_id, lab.mum.secret)
    )
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1


def test_a_burst_of_good_requests_is_cut_by_the_budget_of_the_token(lab: World) -> None:
    _item, digest = volume(lab)
    answers = [pull(lab.mum, digest).status_code for _ in range(700)]
    assert answers[0] == 200 and answers.count(429) > 0
    assert set(answers) <= {200, 429}


def test_a_burst_of_puts_stores_one_row_per_accepted_put_and_never_more(lab: World) -> None:
    _item, digest = volume(lab, count=30)
    ok = sum(
        push(lab.mum, digest, str(1 + n % 30), (1 + n % 30) / 30).status_code == 200
        for n in range(40)
    )
    assert len(positions(lab)) == ok == 40
