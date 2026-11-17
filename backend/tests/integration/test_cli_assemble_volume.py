"""M4 L10 (EF-EXP-019): `shelfhold library assemble-volume`, the real command over a real instance.

An identifier that is not one, a range that is not one and a destination that does not exist are
refused without touching a thing; the chapters of a range are found in reading order, by number,
among the files the destination serves as chapters and nothing else.
"""

from decimal import Decimal

import pytest

from shelfhold.config import Settings
from shelfhold.export.application.assembly import served_chapters
from shelfhold.infrastructure.ids import new_ulid
from tests.assembly_env import exported_chapters, request, service
from tests.export_env import export_env
from tests.l54_env import bench54

pytestmark = pytest.mark.req("EF-EXP-019")


def test_the_command_refuses_what_it_cannot_serve(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        capsys.readouterr()
        gone = new_ulid()
        assert (
            b.cli("library", "assemble-volume", gone, gone, "1", "--first", "1", "--last", "3") == 1
        )
        assert "rien n'a été changé" in capsys.readouterr().out
        for bad in (
            ("not-an-id", gone, "1", "--first", "1", "--last", "3"),
            (gone, gone, "1", "--first", "x", "--last", "3"),
            (gone, gone, "1", "--first", "5", "--last", "3"),
            (gone, gone, "1", "--first", "0", "--last", "99999"),
        ):
            assert b.cli("library", "assemble-volume", *bad) != 0


def test_a_range_is_found_by_number_and_in_reading_order() -> None:
    with export_env() as env:
        chapters = exported_chapters(env, [2, 2, 2, 2, 2])
        edition = chapters.series.edition_id

        with env.engine.connect() as connection:
            middle = served_chapters(
                connection, env.destination_id, edition, Decimal("2"), Decimal("4")
            )
            everything = served_chapters(
                connection, env.destination_id, edition, Decimal("0"), Decimal("99")
            )
            elsewhere = served_chapters(
                connection, new_ulid(), edition, Decimal("0"), Decimal("99")
            )

        assert middle == chapters.items[1:4]
        assert everything == chapters.items
        assert elsewhere == []  # another destination serves nothing of this one's
        done = service(env).assemble(request(env, chapters, items=middle))
        with env.engine.connect() as connection:
            after = served_chapters(
                connection, env.destination_id, edition, Decimal("0"), Decimal("99")
            )
        assert done.export_item_id is not None
        assert after == [chapters.items[0], chapters.items[4]]  # the volume is no chapter
