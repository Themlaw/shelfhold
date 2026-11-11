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
