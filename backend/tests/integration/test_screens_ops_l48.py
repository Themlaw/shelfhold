"""M3 L48 over a real composition (`l41_env.instance`): the two reads behind the screens "Connect a
device" and "Corbeille", from the real tables. The Kavita targets only, their configured address
only (userinfo, query and key left out), a target whose address is no web address is left out, and
the bin list holds nothing that names a path."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, storage
from shelfhold.ops.library import LibraryOps
from tests.l41_env import instance

pytestmark = pytest.mark.req("EF-UX-004", "EF-BIB-021")


def test_device_entry_points_read_the_kavita_targets_and_leave_every_secret_out(
    settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        with inst.engine.begin() as connection:
            for kind, name, url in (
                ("kavita", "source-a", "https://user:pw@kavita.example:8443/?apiKey=abc"),
                (
                    "kavita",
                    "source-b",
                    "javascript:alert(1)",
                ),  # no web address: left out, with a reason
                ("kavita", "source-d", "https://kavita.example:99999"),  # port out of range
                ("tree", "source-c", "https://tree.example"),  # not Kavita: left out
            ):
                connection.execute(
                    sa.insert(export.export_target).values(
                        id=new_ulid(), kind=kind, name=name, config={"url": url}
                    )
                )
        found = LibraryOps(inst.comp).device_entry_points()
    assert [t["name"] for t in found["targets"]] == ["source-a"]
    [target] = found["targets"]
    assert target["kover"] == "https://kavita.example:8443"
    assert target["opds"] == "https://kavita.example:8443/api/opds"
    assert target["kosync"] == "https://kavita.example:8443/api/koreader"
    assert not any(mark in repr(found) for mark in ("pw", "abc", "apiKey", "user:"))
    assert [(s["name"], s["reason"]) for s in found["skipped"]] == [
        ("source-b", "address_unusable"),
        ("source-d", "address_unusable"),
    ]  # one bad row never turns the screen into a 500 and never hides the others


def test_device_entry_points_without_a_target_is_empty(settings: Settings, tmp_path: Path) -> None:
    with instance(settings, tmp_path) as inst:
        assert LibraryOps(inst.comp).device_entry_points() == {"targets": [], "skipped": []}


def test_trash_list_reads_the_bin_index_and_totals_the_bytes(
    settings: Settings, tmp_path: Path
) -> None:
    now = datetime.now(UTC)
    with instance(settings, tmp_path) as inst:
        with inst.engine.begin() as connection:
            for size in (100, 250):
                connection.execute(
                    sa.insert(storage.trash_item).values(
                        id=new_ulid(), kind="unit", ref=new_ulid(), path="entry/file.cbz",
                        size=size, deleted_at=now, purge_after=now + timedelta(days=30),
                        snapshot={"unit_id": "01JUNIT0000000000000000009", "place": "library"},
                        level="delete_content", deletion_id="01JDELE0000000000000000009",
                    )
                )  # fmt: skip
            connection.execute(  # an entry of before the snapshots: no Unit to name, not listed
                sa.insert(storage.trash_item).values(
                    id=new_ulid(), kind="work", ref=new_ulid(), path="old/file.cbz",
                    deleted_at=now, purge_after=now + timedelta(days=30), level="delete_content",
                )
            )  # fmt: skip
        listed = LibraryOps(inst.comp).trash_list()
    [entry] = listed["entries"]
    assert entry["files"] == 2 and entry["bytes"] == 350 and listed["total_bytes"] == 350
    assert entry["irreplaceable"] is False  # the Unit has no row any more: it is gone, not rare
    assert "path" not in repr(listed) and "entry/" not in repr(listed)
