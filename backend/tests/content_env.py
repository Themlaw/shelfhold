"""M5 L05a: what the tests of `/content` share: archives built in memory, a source of archives
that reads nothing from a disk, and the rows that make a carrier exist for a Work."""

from __future__ import annotations

import io
import itertools
import zipfile
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import identity, storage
from tests.integration.test_schema_catalog import add_unit

_NUMBERS = itertools.count(1)  # one Unit number per carrier: an Edition may hold several
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def zip_of(entries: Mapping[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return out.getvalue()


class FakeArchive:
    def __init__(self, entries: Mapping[str, bytes], asked: list[str], pin: str = "fake") -> None:
        self._entries, self._asked, self._pin = dict(entries), asked, pin

    def fingerprint(self) -> str:
        return self._pin

    def names(self) -> frozenset[str]:
        return frozenset(self._entries)

    def read(self, name: str) -> bytes:
        self._asked.append(name)
        return self._entries[name]

    def close(self) -> None:
        return None


class FakeArchives:
    """The port of the archives of a carrier (`ArchiveSource`): content id -> entries. A carrier
    that is not here has no readable copy."""

    def __init__(self) -> None:
        self.by_content: dict[str, Mapping[str, bytes]] = {}
        self.item_content: dict[str, str] = {}
        self.excerpts: dict[str, frozenset[str]] = {}  # content id -> the excerpt the server cuts
        self.pins: dict[str, str] = {}  # content id -> what the file is now (a replaced file)
        self.asked: list[str] = []

    def open(self, carrier_type: str, carrier_id: str, scope: str = "full") -> FakeArchive:
        from shelfhold.reading.application.content_access import (
            ExcerptUnavailableError,
            NoReadableCopyError,
        )

        content_id = (
            self.item_content.get(carrier_id)
            if carrier_type == "export_item"
            else (carrier_id if carrier_type == "content" else None)
        )
        if content_id is None or content_id not in self.by_content:
            raise NoReadableCopyError
        entries = self.by_content[content_id]
        if scope == "excerpt":  # the source's own bound; none known: « extrait indisponible »
            if content_id not in self.excerpts:
                raise ExcerptUnavailableError
            entries = {n: b for n, b in entries.items() if n in self.excerpts[content_id]}
        return FakeArchive(entries, self.asked, self.pins.get(content_id, "fake"))


def add_carrier(engine: sa.Engine, edition_id: str, *, format: str = "cbz") -> str:
    """A `content` of a Unit of the Edition, adopted in place (no Variant, no path): the carrier
    id the tickets are issued for."""
    with write_transaction(engine) as connection:
        unit_id = add_unit(connection, edition_id, str(next(_NUMBERS)))
        content_id = new_ulid()
        connection.execute(
            sa.insert(storage.content).values(
                id=content_id, unit_id=unit_id, variant_id=None, kind="images",
                media_type="application/vnd.comicbook+zip", format=format,
                retention="adopted_ref", path=None, original_sha256="sha256:" + "0" * 64,
                store_provenance="received", size=1,
            )
        )  # fmt: skip
    return content_id


def session_id_of(engine: sa.Engine, username: str) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                sa.select(identity.session.c.id)
                .join(identity.user, identity.user.c.id == identity.session.c.user_id)
                .where(
                    identity.user.c.username == username, identity.session.c.revoked_at.is_(None)
                )
                .order_by(identity.session.c.created_at.desc())
            ).scalar_one()
        )


def user_id_of(engine: sa.Engine, username: str) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                sa.select(identity.user.c.id).where(identity.user.c.username == username)
            ).scalar_one()
        )


BOOK: dict[str, bytes] = {
    "OEBPS/ch1.xhtml": b"<html xmlns='http://www.w3.org/1999/xhtml'><body><p>one</p></body></html>",
    "OEBPS/ch2.xhtml": b"<html xmlns='http://www.w3.org/1999/xhtml'><body><p>two</p></body></html>",
    "OEBPS/style.css": b"p { color: black }",
    "OEBPS/cover.png": PNG,
    "OEBPS/p1.svg": b"<svg xmlns='http://www.w3.org/2000/svg'><script>x()</script></svg>",
    "OEBPS/x.js": b"alert(1)",
    "OEBPS/m.mjs": b"export default 1",
    "OEBPS/m.wasm": b"\x00asm\x01\x00\x00\x00",
    "OEBPS/data.json": b"{}",
    "OEBPS/page.html": b"<script>x()</script>",
    "OEBPS/font.woff2": b"wOF2",
    "OEBPS/blob.php": b"<?php x(); ?>",
}


def new_source(**extra: Any) -> FakeArchives:
    source = FakeArchives()
    for key, value in extra.items():
        source.by_content[key] = value
    return source
