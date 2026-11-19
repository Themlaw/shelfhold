"""M5 L07 (EF-LEC-014, EF-EXP-026): what the reader writes is not sent to Kavita as a page.

A position of the integrated reader on an EPUB is a `cfi` and a progression: a place in a text that
reflows, not a page. Until L09 says how an EPUB goes out, the pass of the synchronisation neither
pushes it nor counts it as a mark of reading; a page position on the same file still goes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.content_trust import CONTENT_FILTER
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import reading
from tests.sync_env import sync_env

CFI = {
    "v": 1, "type": "cfi", "cfi": "epubcfi(/6/4!/4/2/1:0)", "totalProgression": 0.9,
    "filter": CONTENT_FILTER,
}  # fmt: skip


@pytest.mark.req("EF-LEC-014", "EF-EXP-026")
def test_reader_epub_anchor_is_never_pushed_to_kavita(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1, pages=20)
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
        env.clock.advance(minutes=1)
        assert env.sync(reader).code == "ok"
        assert env.fake.progress_writes == []  # nothing said to Kavita of the EPUB anchor
        assert env.kavita_page(reader, chapter) == 0
        env.device_position(reader, chapter, 6)  # a page position of a device on the same file
        env.clock.advance(minutes=1)
        env.sync(reader)
        assert env.kavita_page(reader, chapter) == 6  # a page still goes: only the anchor is held
