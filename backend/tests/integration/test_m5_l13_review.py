"""The adverse reading of M5 L13 (EF-LEC-006, EF-USR-013, EF-BIB-052): what the review found.

Every test here is a hole found by reading the lot against its own claims: red on the branch
(`xfail(strict=True)`), it fails for the reason its message says, and it turns loud (XPASS) the
day the hole is closed. What the same reading found solid stays covered by the lot's own tests:
the choices of a profile never leave it (a device name is a label inside the profile, the rows
of another profile are never read nor written, and a key, a token or a forwarded proxy gets no
answer); a Work hidden from a child answers like one that is not there on every door, the write
of a row included; the manifest names its series only once the door has said the profile may
see it, and an identifier that is not one shape answers before anything is read; the choices are
closed lists of bounded size; a write without the CSRF token is refused; the migration is
frozen for the minor line (its fingerprint holds), its head is pinned on 0102 and its rows
follow their profile (`test_schema_migration_0103`). The keyboard holds its claims too: no
shortcut acts inside a field of the settings, Escape gives the focus back from anywhere (no
trap), every command has a name, and the catalogue speaks both languages.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.identity.adapters.store import SqlIdentityStore
from shelfhold.identity.application.delete_user import MAX_EXPORT_ROWS, build_export
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, reading
from shelfhold.reading.application.settings import InvalidSettingError, ReaderSettings
from tests.reading_env import NEW_HASH, reading_env


@pytest.mark.req("EF-USR-013")
@pytest.mark.xfail(strict=True)
def test_the_export_of_a_member_holds_her_reading_settings(tmp_path: Path) -> None:
    """`reader_settings` points at a profile by a foreign key: her export (EF-USR-013,
    "everything that is hers") must hold it. `SECTIONS` names no `reader_settings` and the store
    reads no such section, so the choices she made in the reader are missing from the document
    the instance hands back — `test_profile_export_coverage` already fails on the branch for the
    same table."""
    with reading_env(tmp_path) as env:
        reader = env.user("lectrice")
        service = ReaderSettings(env.engine)
        series = env.seed.works["ok"]
        service.set(user_id=reader, kind="direction", key=series, value="rtl", scope="everywhere")
        service.set(
            user_id=reader,
            kind="comfort",
            key="fit",
            value="width",
            scope="device",
            device_id="web-lecture",
        )
        store = SqlIdentityStore(env.engine)
        profile = store.profile(reader)
        rows = store.export_rows(reader, limit=MAX_EXPORT_ROWS)
        assert profile is not None and rows is not None
        held = build_export(profile, rows).get("reader_settings")
        assert held is not None, (
            "the export of a member holds no reader_settings section: her choices are hers"
        )
        assert {(row["kind"], row["key"], row["value"]) for row in held} == {
            ("direction", series, "rtl"),
            ("comfort", "fit", "width"),
        }


@pytest.mark.req("EF-BIB-052")
@pytest.mark.xfail(strict=True)
def test_a_purged_work_takes_its_pinned_settings_with_it(tmp_path: Path) -> None:
    """`PurgeService._erase_rows` is the step of `purge --now` that takes the rows of a Work out
    of the base, and it takes the anchors and the proposals of the Work's files (EF-BIB-052:
    nothing of the Work stays behind). A `reader_settings` row pinned to the Œuvre is a row of
    the same Work — it names it by its identifier — and the purge leaves it behind: one row per
    reader who ever pinned this series, kept for good after the Work is gone."""
    from shelfhold.export.application.purge import PurgeService

    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        unit = env.units("ok", 1)[0]
        item = env.file("ok", [unit], content_hash=NEW_HASH)
        with env.engine.connect() as connection:
            content = str(
                connection.execute(
                    sa.select(export.export_item.c.content_id).where(
                        export.export_item.c.id == item
                    )
                ).scalar_one()
            )
        work = env.seed.works["ok"]
        ReaderSettings(env.engine).set(
            user_id=reader, kind="direction", key=work, value="rtl", scope="everywhere"
        )
        plan = type("Plan", (), {"unit_ids": (unit,), "items": (item,), "contents": (content,)})()
        with write_transaction(env.engine) as connection:
            PurgeService._erase_rows(connection, plan, allowed=None)
        with env.engine.connect() as connection:
            kept = connection.execute(
                sa.select(sa.func.count())
                .select_from(reading.reader_setting)
                .where(reading.reader_setting.c.key == work)
            ).scalar_one()
        assert kept == 0, "a setting pinned to a purged Work outlives the Work"


@pytest.mark.req("EF-LEC-006")
@pytest.mark.xfail(strict=True)
def test_a_direction_written_for_one_device_does_not_decide_for_another(tmp_path: Path) -> None:
    """The portée is part of the write: the route takes « pour cet appareil » for any kind of
    choice. A direction written for one device is kept as such, then read as if it had been
    written « partout »: `resolve` never looks at the scope of a direction row, so a choice
    asked for one device decides what every device of the profile reads. The portée must be
    refused at the write or honored at the read — either one closes this hole."""
    with reading_env(tmp_path) as env:
        reader = env.user("lectrice")
        service = ReaderSettings(env.engine)
        series = env.seed.works["ok"]
        # a portée a direction does not take, refused at the write: an equally closed hole
        with contextlib.suppress(InvalidSettingError):
            service.set(
                user_id=reader,
                kind="direction",
                key=series,
                value="rtl",
                scope="device",
                device_id="web-tablette",
            )
        elsewhere = service.resolve(user_id=reader, series_id=series, device_id="web-telephone")
        assert elsewhere.direction is None, (
            "a direction written « pour cet appareil » decides for another device"
        )


@pytest.mark.req("EF-LEC-006")
@pytest.mark.xfail(strict=True)
def test_a_choice_is_not_anchored_to_a_work_that_is_not_there(tmp_path: Path) -> None:
    """A direction is « a series named by its identifier »: a key that is not any Work at all is
    taken all the same, and nothing bounds how many rows a profile may hold — one write per
    unknown identifier, each kept for good, and every `resolve` reads them all on every door of
    the reader. A key that names nothing must leave nothing behind. (A profile that names a
    series she cannot see keeps the lot's own rule — one answer for the hidden and the absent —
    so this stays a question of rows kept, never one of existence told.)"""
    with reading_env(tmp_path) as env:
        reader = env.user("lectrice")
        service = ReaderSettings(env.engine)
        nowhere = new_ulid()  # a well-shaped identifier of nothing
        service.set(user_id=reader, kind="direction", key=nowhere, value="rtl", scope="everywhere")
        with env.engine.connect() as connection:
            kept = connection.execute(
                sa.select(sa.func.count())
                .select_from(reading.reader_setting)
                .where(reading.reader_setting.c.key == nowhere)
            ).scalar_one()
        assert kept == 0, (
            "a choice anchored to a Work that is not there is kept, and rows pile up unbounded"
        )
