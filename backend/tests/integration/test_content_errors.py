"""L15: content errors counted per upstream source, irreplaceable candidates (EF-BIB-042, 031).

Only an error of content (the source says the chapter is gone) is counted; two in a row on the
same binding make the Units it holds candidates. An error of authentication, of the network or a
5xx pauses or waits and counts nothing. A candidate is a name on a list: nothing is copied,
moved or deleted because of it (the safeguard is L16).
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from shelfhold.acquisition.application.failures import FailureLedger
from shelfhold.library.domain.irreplaceable import CANDIDATE_AFTER, is_candidate
from shelfhold.library.public import content_error_candidates
from shelfhold.sdk.fetcher import Response
from shelfhold.sdk.source import ContentRemoved, TemporarilyUnavailable
from tests.fakes.suwayomi import FakeSuwayomi
from tests.follow_env import Bench, follow_bench

pytestmark = pytest.mark.req("EF-BIB-042", "EF-BIB-031")

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _file_one(bench: Bench) -> None:
    """Follow, then file chapter 3: the binding now holds a Unit with a copy."""
    bench.run()
    bench.add_chapter(3)
    for _ in range(4):
        bench.run()
    assert bench.library_files()


def _gone(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fetch(f: Any, ticket: Any) -> Any:
        raise ContentRemoved("the chapter is gone from the server")

    monkeypatch.setattr(bench.service, "fetch", fetch)


def _candidates(bench: Bench) -> list[Any]:
    with bench.lib.engine.connect() as connection:
        return content_error_candidates(connection)


def _lose_chapter(bench: Bench, number: int) -> None:
    bench.add_chapter(number)
    for _ in range(4):
        bench.run()


def _errors(bench: Bench) -> list[int]:
    rows = bench.rows("SELECT consecutive_content_errors AS n FROM source_failure")
    return [r["n"] for r in rows]


def test_the_threshold_is_two_consecutive_content_errors() -> None:
    assert CANDIDATE_AFTER == 2
    assert [is_candidate(n) for n in (0, 1, 2, 3)] == [False, False, True, True]


def test_two_consecutive_content_errors_mark_irreplaceable_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with follow_bench() as bench:
        _file_one(bench)
        _gone(bench, monkeypatch)
        _lose_chapter(bench, 4)
        assert _errors(bench) == [1] and _candidates(bench) == []  # once is not enough
        _lose_chapter(bench, 5)
        assert _errors(bench) == [2]
        found = _candidates(bench)
        assert len(found) == 1 and found[0].errors == 2
        assert found[0].last_error_code == "source.content_removed"
        # only a name on a list: the copy is where it was, and the Unit is still there
        assert bench.library_files()
        assert bench.rows("SELECT id FROM unit WHERE id = :u", u=found[0].unit_id)


def test_a_success_resets_the_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    with follow_bench() as bench:
        _file_one(bench)
        _gone(bench, monkeypatch)
        _lose_chapter(bench, 4)
        _lose_chapter(bench, 5)
        assert len(_candidates(bench)) == 1
        monkeypatch.undo()  # the source answers again
        _lose_chapter(bench, 6)
        assert _errors(bench) == [0] and _candidates(bench) == []


def test_content_errors_counted_per_upstream_source() -> None:
    with follow_bench() as bench:
        _file_one(bench)
        other = bench.add_binding("other-key", "remote-2")
        with bench.lib.engine.begin() as connection:
            for key in ("alpha-key", "alpha-key", "second-key"):
                FailureLedger.count_content(connection, bench.binding_id, key, "x", NOW)
            FailureLedger.count_content(connection, other, "other-key", "x", NOW)
        rows = bench.rows(
            "SELECT limit_key, consecutive_content_errors AS n FROM source_failure "
            "ORDER BY limit_key"
        )
        assert {(r["limit_key"], r["n"]) for r in rows} == {
            ("alpha-key", 2),
            ("second-key", 1),
            ("other-key", 1),
        }
        # the failing source is the binding's own: a key it does not use is nobody's candidate
        assert _candidates(bench) == []
        key = bench.rows(
            "SELECT i.source_key AS k FROM source_binding b JOIN source_item i "
            "ON i.id = b.source_item_id WHERE b.id = :b",
            b=bench.binding_id,
        )[0]["k"]
        with bench.lib.engine.begin() as connection:
            FailureLedger.count_content(connection, bench.binding_id, key, "x", NOW)
            FailureLedger.count_content(connection, bench.binding_id, key, "x", NOW)
        assert len(_candidates(bench)) == 1  # the other binding holds nothing: untouched


def test_suwayomi_401_three_cycles_creates_no_candidate() -> None:
    server = FakeSuwayomi(auth_mode="basic_auth")
    with follow_bench(server, has_credentials=True) as bench:
        for _ in range(3):
            assert bench.run().paused == "auth_expired"
        assert bench.rows("SELECT * FROM source_failure") == []
        assert _candidates(bench) == []
        assert bench.rows("SELECT * FROM download_request") == []


def test_network_and_5xx_pause_without_counting(monkeypatch: pytest.MonkeyPatch) -> None:
    with follow_bench() as bench:
        _file_one(bench)
        bench.add_chapter(4)
        bench.run()
        real = bench.server._download

        def unavailable(path: str) -> Response:
            return Response(status=503, headers={}, body=b"")

        monkeypatch.setattr(bench.server, "_download", unavailable)
        for _ in range(4):
            bench.run()
        monkeypatch.setattr(bench.server, "_download", real)

        async def asleep(f: Any, ticket: Any) -> Any:
            raise TemporarilyUnavailable("the server is asleep")

        monkeypatch.setattr(bench.service, "fetch", asleep)
        for _ in range(4):
            bench.run()
        assert bench.rows("SELECT * FROM source_failure") == []
        assert _candidates(bench) == []


def test_candidates_are_listed_by_status(monkeypatch: pytest.MonkeyPatch) -> None:
    from shelfhold.interfaces.cli.status import report_lines

    with follow_bench() as bench:
        _file_one(bench)
        _gone(bench, monkeypatch)
        _lose_chapter(bench, 4)
        _lose_chapter(bench, 5)
        with bench.lib.engine.connect() as connection:
            from shelfhold.ops.status import candidate_report

            report = candidate_report(connection)
        assert [r["unit_id"] for r in report] == [c.unit_id for c in _candidates(bench)]
        lines = report_lines({"content_error_candidates": report})
        assert len(lines) == 1 and report[0]["unit_id"] in lines[0]


def test_the_counter_write_is_one_atomic_statement() -> None:
    with follow_bench() as bench:
        _file_one(bench)
        with bench.lib.engine.begin() as connection:
            for _ in range(5):
                FailureLedger.count_content(connection, bench.binding_id, "k", "x", NOW)
        assert [r["n"] for r in bench.rows(
            "SELECT consecutive_content_errors AS n FROM source_failure")] == [5]  # fmt: skip
