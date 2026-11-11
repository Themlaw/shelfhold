"""M2 L04, review of the PR: regressions for each blocking finding. Credentials are built at run
time (`synthetic_text`), none is a literal."""

from collections.abc import Iterator
from urllib.parse import quote

import pytest

from shelfhold.infrastructure.logging import register_secret_value
from tests.follow_env import Bench, follow_bench, new_chapter
from tests.redaction_support import forget_secret_values, synthetic_text

KEY = synthetic_text("review-key", specials=True)


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    forget_secret_values()
    yield
    forget_secret_values()


def _keyed(key: str, number: int) -> str:
    return f"/series-alpha/{quote(key, safe='')}/ch-{number:03d}"


def _carry(bench: Bench, key: str) -> None:
    for number, chapter in enumerate(bench.alpha()["chapters"], start=1):
        chapter["url"] = _keyed(key, number)


@pytest.mark.req("EF-SRC-035")
def test_a_rewritten_reference_still_downloads_the_file() -> None:
    """The stored form holds `{secret}`; what goes back to the plugin holds the key again, so the
    plugin finds its own URL and the archive is received (review: the download was abandoned)."""
    register_secret_value(KEY)
    with follow_bench() as bench:
        bench.fetcher.ref_template = "{secret}"  # type: ignore[attr-defined]
        bench.fetcher.ref_secret = lambda: KEY  # type: ignore[attr-defined]
        _carry(bench, KEY)
        bench.run()  # the baseline: what is there is known
        bench.alpha()["chapters"].append(new_chapter(5030, 3, url=_keyed(KEY, 3)))
        report = bench.run()
        assert report.requested == 1 and report.paused is None
        report = bench.run()
        assert report.fetched == 1, report
        assert bench.library_files()  # the archive was received and filed
        assert bench.rows("SELECT * FROM source_failure WHERE failure_class = 'content'") == []


UNREG = synthetic_text("review-unreg")[:16]


@pytest.mark.req("EF-SRC-035")
@pytest.mark.parametrize(
    "url", ["/manga/secret/chapter-{n}", "/title/{n}?session=2024", "/r?sid=42&c={n}"]
)
def test_a_url_that_only_looks_like_a_credential_pauses_nothing(url: str) -> None:
    """Review: `/manga/secret/...` or `?session=2024` paused the whole service."""
    with follow_bench() as bench:
        for number, chapter in enumerate(bench.alpha()["chapters"], start=1):
            chapter["url"] = url.format(n=number)
        report = bench.run()
        assert report.paused is None


@pytest.mark.req("EF-SRC-035")
def test_a_unit_with_a_secret_is_quarantined_and_the_service_goes_on() -> None:
    """One Unit's reference holds an unregistered token: it is left out, visibly (health and one
    `source.ref_has_secret`), the others are followed, the service is not paused, and the token is
    nowhere in the database."""
    with follow_bench() as bench:
        chapters = bench.alpha()["chapters"]
        chapters[0]["url"] = f"/series-alpha/ch-001?accessToken={UNREG}"
        report = bench.run()
        assert report.paused is None
        health = bench.rows("SELECT health FROM source_config")[0]["health"]
        assert "quarantine" in str(health) and UNREG not in str(health)
        events = bench.rows("SELECT type FROM event WHERE type = 'source.ref_has_secret'")
        assert len(events) == 1
        bench.run()
        assert len(bench.rows("SELECT type FROM event WHERE type = 'source.ref_has_secret'")) == 1
        raw = bench.lib.engine.raw_connection()
        try:
            dump = "\n".join(raw.driver_connection.iterdump())  # type: ignore[attr-defined]
        finally:
            raw.close()
        assert UNREG not in dump


@pytest.mark.req("EF-EXP-056", "EF-SRC-035")
@pytest.mark.parametrize(
    "name",
    ["auth", "session", "sid", "authorization", "credentials", "pwd", "key", "bearer", "pass"],
)
def test_the_snapshot_schema_is_closed_whatever_the_name_of_the_key(name: str) -> None:
    """A denylist of words let `auth`, `sid`, `pwd`... through; only declared keys pass (review)."""
    from shelfhold.acquisition.application.upstream_snapshot import (
        SnapshotRefusedError,
        validate_payload,
    )
    from shelfhold.plugin_host.adapters.meta.suwayomi.service import SuwayomiService

    schema = SuwayomiService.snapshot_schema
    ok = {"categories": [{"id": 1, "name": "Reading", "include_in_update": "INCLUDE"}]}
    assert validate_payload({**ok, "update_strategy": "ALWAYS_UPDATE"}, schema=schema)
    with pytest.raises(SnapshotRefusedError):
        validate_payload({**ok, name: synthetic_text("snap")}, schema=schema)
    with pytest.raises(SnapshotRefusedError):  # a key nested where the schema has none
        validate_payload(
            {"categories": [{"id": 1, "name": "x", "include_in_update": "a", name: "v"}]},
            schema=schema,
        )
    with pytest.raises(SnapshotRefusedError):  # no schema declared: nothing is kept
        validate_payload(ok, schema=None)
