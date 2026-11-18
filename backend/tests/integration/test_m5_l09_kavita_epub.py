"""M5 L09 (EF-LEC-009, EF-EXP-026, EF-USR-012, #292): an EPUB goes to Kavita by its share alone.

Kavita counts an EPUB in documents (`pageNum` is a document of its spine), Shelfhold in shares: a
page of one is not a page of the other. So an EPUB is never given as a page. What a device wrote
(an xpointer and a share) is relayed through Kavita's own kosync route, with the member's own key,
forward only; the xpointer goes as the device wrote it (Kavita converts it, ADR-0053 section 6) and
nothing computed here (no page, no index of the spine) goes at all. What the reader of the web wrote
(a `cfi`) is not an xpointer and is never relayed.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, reading
from tests.sync_env import Chapter, SyncEnv, sync_env

pytestmark = pytest.mark.req("EF-LEC-009", "EF-EXP-026", "EF-USR-012")

DIGEST = "0123456789abcdef0123456789abcdef"
XPOINTER = "/body/DocFragment[7]/body/div[2]/p[3]/text().11"
CFI = {
    "v": 1, "type": "cfi", "cfi": "epubcfi(/6/4!/4/2/1:0)", "totalProgression": 0.9,
    "filter": "xhtml-neutralizer@v1",
}  # fmt: skip


def epub_chapter(env: SyncEnv, series: str = "Orchard Road", documents: int = 12) -> Chapter:
    """An EPUB the shelf holds and Kavita indexed: its `pages` are documents."""
    chapter = env.chapter(series, 1, pages=documents, ext="epub")
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.insert(export.document_alias).values(
                export_item_id=chapter.carrier, file_hash="sha256:" + "ab" * 32,
                content_hash="sha256:" + "ab" * 32, koreader_partial_md5="md5:" + DIGEST,
            )
        )  # fmt: skip
    return chapter


def device_xpointer(
    env: SyncEnv, user: str, chapter: Chapter, share: float, *, xpointer: str = XPOINTER,
    device: str = "kobo", extra: dict[str, object] | None = None,
) -> None:  # fmt: skip
    """What KOReader's PUT left (`Kosync.push`): an xpointer, its index, a share, its writer."""
    now = env.clock.now()
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.insert(reading.reading_position).values(
                id=new_ulid(), user_id=user, carrier_type="export_item", carrier_id=chapter.carrier,
                content_hash=env.content_hash(chapter),
                locator={"xpointer": xpointer, "spine": 6, "device": "Libra 2",
                         "who": "d:" + device, **(extra or {})},
                percent=share, client_ts=now, server_ts=now, device_id=device, source="device",
                covered_units=chapter.units,
            )
        )  # fmt: skip


def test_epub_sync_with_kavita_by_percent_only(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = epub_chapter(env, documents=12)
        device_xpointer(env, reader, chapter, 0.5)
        outcome = env.sync(reader)
        assert outcome.pushed == 1
        (write,) = env.fake.kosync_writes
        assert (write["account"], write["document"]) == (env.names[reader], DIGEST)
        assert write["percentage"] == 0.5  # the share, as the device said it
        assert write["progress"] == XPOINTER  # opaque: the text the device wrote, to the character
        # nothing computed here goes: no page, no document count, no index of the spine
        assert set(write["body"]) <= {"document", "progress", "percentage", "device", "device_id",
                                      "timestamp"}  # fmt: skip
        assert not env.fake.requests_to("/api/reader/progress")
        assert env.fake.progress_writes == [] and env.kavita_page(reader, chapter) == 0
        for request in env.fake.requests_to("/api/Koreader"):
            assert env.keys[reader] in request.path and "x-api-key" not in request.headers
        assert env.fake.violations == []
        # nothing new: nothing is sent again
        env.clock.advance(timedelta(minutes=5))
        assert env.sync(reader).pushed == 0 and len(env.fake.kosync_writes) == 1


def test_epub_share_goes_forward_only(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = epub_chapter(env)
        env.fake.read_in_kavita_epub(env.names[reader], DIGEST, 0.8)  # read further in Kavita
        device_xpointer(env, reader, chapter, 0.5)
        assert env.sync(reader).pushed == 0 and env.fake.kosync_writes == []
        env.clock.advance(timedelta(minutes=5))
        device_xpointer(env, reader, chapter, 0.9)
        assert env.sync(reader).pushed == 1
        assert [w["percentage"] for w in env.fake.kosync_writes] == [0.9]


def test_epub_unknown_to_kavita_is_a_start_not_an_error(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = epub_chapter(env)
        device_xpointer(env, reader, chapter, 0.3)  # Kavita answers 400 to a book it holds no state of
        assert env.sync(reader).pushed == 1
        assert [w["percentage"] for w in env.fake.kosync_writes] == [0.3]


def test_epub_reader_anchor_is_not_an_xpointer_and_is_never_relayed(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = epub_chapter(env)
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.insert(reading.reading_position).values(
                    id=new_ulid(), user_id=reader, carrier_type="export_item",
                    carrier_id=chapter.carrier, content_hash=env.content_hash(chapter),
                    locator=CFI, percent=0.9, client_ts=env.clock.now(),
                    server_ts=env.clock.now(), device_id="phone", source="device",
                    covered_units=chapter.units,
                )
            )  # fmt: skip
        assert env.sync(reader).pushed == 0
        assert env.fake.kosync_writes == [] and not env.fake.requests_to("/api/reader/progress")


def test_epub_never_pushed_as_a_page_even_when_read_to_the_end(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = epub_chapter(env, documents=12)
        device_xpointer(env, reader, chapter, 1.0, extra={"end": True})
        env.sync(reader)
        assert not env.fake.requests_to("/api/reader/progress")
        assert [w["percentage"] for w in env.fake.kosync_writes] == [1.0]  # « finished », as a share


def test_epub_each_member_is_relayed_with_their_own_key_and_nobody_elses(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        one, two = env.member("one"), env.member("two")
        chapter = epub_chapter(env)
        device_xpointer(env, one, chapter, 0.4)
        device_xpointer(env, two, chapter, 0.7, xpointer=XPOINTER.replace("[7]", "[9]"))
        env.sync_all()
        by_account = {w["account"]: w for w in env.fake.kosync_writes}
        assert by_account[env.names[one]]["percentage"] == 0.4
        assert by_account[env.names[two]]["percentage"] == 0.7
        assert by_account[env.names[two]]["progress"].startswith("/body/DocFragment[9]")


def test_epub_revoked_key_relays_nothing_and_falls_back_on_no_other_key(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = epub_chapter(env)
        device_xpointer(env, reader, chapter, 0.5)
        env.revoke(reader)
        env.sync(reader)
        assert env.fake.kosync_writes == [] and not env.fake.requests_to("/api/Koreader")


def test_epub_chapter_of_another_file_under_the_same_digest_is_never_guessed(tmp_path: Path) -> None:
    """A file with no digest (no alias) has no kosync identity: nothing is sent for it."""
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 2, pages=12, ext="epub")  # no alias
        device_xpointer(env, reader, chapter, 0.5)
        env.sync(reader)
        assert env.fake.kosync_writes == []
