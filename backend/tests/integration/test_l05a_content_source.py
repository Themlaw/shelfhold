"""M5 L05a (EF-LEC-005, ADR-0047 I35 extended): what `/content` reads of a library.

Real files and a real database. An `untrusted` EPUB is read by its neutralized derivative, never by
the original kept in /store, and a library file that is not the recorded derivative serves nothing.
"""

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.schema import storage
from shelfhold.library.public import ReadableCopies
from shelfhold.reading.application.content_access import NoReadableCopyError
from tests.integration.test_untrusted_epub_pipeline import TRAPPED, content_row, ingest
from tests.library_env import lib_env


@pytest.mark.req("EF-LEC-005", "EF-BIB-051")
def test_content_never_serves_untrusted_original() -> None:
    with lib_env() as env:
        _result, library = ingest(env)
        row = content_row(env)
        copies = ReadableCopies(env.engine, lambda: env.fsops)
        archive = copies.open("content", row.id)
        try:
            names = archive.names()
            assert names, "the derivative has entries"
            body = b"".join(archive.read(n) for n in sorted(names))
        finally:
            archive.close()
        assert b"<script" not in body and b"alert(1)" not in body  # the neutralized derivative
        (kept,) = env.files_under(env.store)
        assert kept.read_bytes() == TRAPPED  # the original is in /store, untouched ...
        # ... and it is never what a ticket reads, even if the library holds the original
        library.write_bytes(TRAPPED)
        with pytest.raises(NoReadableCopyError):
            copies.open("content", row.id)
        # ... nor when there is no derivative recorded at all
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(storage.content).values(
                    derived_hash=None, derived_by=None, derived_hash_by=None
                )
            )
        with pytest.raises(NoReadableCopyError):
            copies.open("content", row.id)


@pytest.mark.req("EF-LEC-005")
def test_export_item_resolves_to_its_content_and_absent_carriers_to_nothing() -> None:
    with lib_env() as env:
        _result, _library = ingest(env)
        copies = ReadableCopies(env.engine, lambda: env.fsops)
        for carrier in (
            ("content", "01HZZZZZZZZZZZZZZZZZZZZZZZ"),
            ("export_item", "01HZZZZZZZZZZZZZZZZZZZZZZZ"),
            ("other", content_row(env).id),
        ):
            with pytest.raises(NoReadableCopyError):
                copies.open(*carrier)


@pytest.mark.req("EF-LEC-005")
def test_a_page_archive_is_read_from_its_library_copy() -> None:
    from tests.export_env import export_env

    with export_env() as env:
        series = env.main_series()
        unit = env.add_chapter(series, "77")
        with env.engine.connect() as connection:
            content_id = connection.execute(
                sa.select(storage.content.c.id).where(storage.content.c.unit_id == unit)
            ).scalar_one()
        copies = ReadableCopies(env.engine, lambda: env.lib.fsops)
        archive = copies.open("content", content_id)
        try:
            assert any(name.endswith(".png") for name in archive.names())
            name = sorted(archive.names())[0]
            assert archive.read(name).startswith(b"\x89PNG")
            with pytest.raises(KeyError):
                archive.read("../../etc/passwd")  # only the names of the archive are readable
        finally:
            archive.close()
