"""A followed Edition with two ranked bindings: Series Alpha (rank 1) and Series Beta (rank 2)."""

from datetime import timedelta
from typing import Any

import sqlalchemy as sa

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition
from tests.follow_env import START, Bench

ALPHA, BETA = 0, 1  # the position of the series in the simulated server


def chapters(
    series: int, numbers: list[int], team: str = "A", *, days_ago: float = 1.0
) -> list[dict[str, Any]]:
    prefix, base = ("/series-alpha", 6000) if series == ALPHA else ("/series-beta", 7000)
    stamp = str(int((START - timedelta(days=days_ago)).timestamp() * 1000))
    return [
        {
            "id": base + number * 10 + ord(team) - 64, "url": f"{prefix}/ch-{number:03d}-{team}",
            "name": f"Chapter {number}", "chapterNumber": float(number), "sourceOrder": number - 1,
            "scanlator": f"Team {team}", "uploadDate": stamp,
            "cbz": "comic_a_001.cbz" if series == ALPHA else "comic_a_002.cbz",
        }
        for number in numbers
    ]  # fmt: skip


def serve(bench: Bench, series: int, items: list[dict[str, Any]]) -> None:
    bench.server.state["mangas"][series]["chapters"][:] = items


def bind_beta(bench: Bench, *, priority: int = 2) -> str:
    """Series Beta becomes a second binding of the bench's Edition, at `priority`."""
    item_id, binding_id = new_ulid(), new_ulid()
    with write_transaction(bench.lib.engine) as connection:
        connection.execute(
            sa.insert(acquisition.source_item).values(
                id=item_id, plugin_id="suwayomi", source_key="suwayomi:7002",
                remote_id="/series-beta",
            )
        )  # fmt: skip
        connection.execute(
            sa.insert(acquisition.source_binding).values(
                id=binding_id, work_id=bench.work_id, edition_id=bench.edition_id,
                source_item_id=item_id, priority=priority, active=True,
            )
        )  # fmt: skip
    return binding_id


def watermark(bench: Bench, number: str = "0") -> None:
    with write_transaction(bench.lib.engine) as connection:
        connection.execute(
            sa.text(
                "UPDATE series_follow SET follow_from_segment = '', follow_from_number = :n, "
                "follow_from_norm_by = 'number@1'"
            ),
            {"n": number},
        )


def policy(bench: Bench, value: str) -> None:
    with write_transaction(bench.lib.engine) as connection:
        connection.execute(sa.text("UPDATE series_follow SET upgrade_policy = :p"), {"p": value})


def settle(bench: Bench) -> None:
    for _ in range(6):
        bench.run()
    assert not bench.rows(
        "SELECT id FROM download_request WHERE state IN ('requested','downloaded')"
    )


def requests(bench: Bench) -> list[tuple[str, str, str]]:
    """(number, series asked, state) of every ticket, oldest first."""
    return [
        (r["n"], r["remote_id"].removeprefix("/series-"), r["state"])
        for r in bench.rows(
            "SELECT u.number_norm AS n, si.remote_id AS remote_id, r.state AS state "
            "FROM download_request r JOIN unit_variant v ON v.id = r.variant_id "
            "JOIN unit u ON u.id = v.unit_id JOIN source_binding b ON b.id = r.source_binding_id "
            "JOIN source_item si ON si.id = b.source_item_id ORDER BY u.number_sort, r.rowid"
        )
    ]
