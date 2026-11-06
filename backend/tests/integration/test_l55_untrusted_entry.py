"""L55 (EF-BIB-051 (6)): a CBZ from Suwayomi with an entry off the allowlist is refused.

The chapter goes through the whole follow cycle: the simulated Suwayomi serves the archive, the
download ticket receives it, the production store audits it. Nothing is stubbed between the two.
"""

from pathlib import Path

import pytest

from tests.follow_env import Bench, follow_bench
from tests.library_env import PNG, cbz_bytes

pytestmark = pytest.mark.req("EF-BIB-051")

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def serve(bench: Bench, archive: Path) -> None:
    bench.add_chapter(3)
    bench.alpha()["chapters"][-1]["cbz"] = str(archive)  # an absolute path wins over the fixtures


def downloads(bench: Bench) -> int:
    return len(bench.server.requests_to("/chapter/"))


@pytest.mark.parametrize(
    "pages",
    [
        (("001.png", PNG), ("002.svg", SVG)),
        (("001.png", PNG), ("002.jpg", b"<html><script>alert(1)</script></html>")),
    ],
    ids=["svg-entry", "html-behind-a-jpg-extension"],
)
def test_untrusted_cbz_entry_never_retried_emits_event_no_content_failure(
    tmp_path: Path, pages: tuple[tuple[str, bytes], ...]
) -> None:
    archive = tmp_path / "hostile.cbz"
    archive.write_bytes(cbz_bytes(pages))
    with follow_bench() as bench:
        bench.run()  # baseline
        serve(bench, archive)
        for _ in range(6):  # more cycles than a retry budget would need
            bench.run()

        assert bench.library_files() == []  # the Unit is not filed, so never exported
        (request,) = bench.rows("SELECT state, attempts FROM download_request")
        assert request["state"] == "abandoned"  # closed for good, not asked again
        assert downloads(bench) == 1  # the archive was fetched once and never retried
        assert bench.rows("SELECT * FROM source_failure") == []  # no content failure counted
        assert bench.rows("SELECT count(*) AS n FROM content")[0]["n"] == 0
        (event,) = bench.events("content.untrusted_entry")  # the admin is told, once
        assert "title" not in str(event).lower() and "hostile" not in str(event)
        assert bench.binding()["active"]  # the series is not disabled by it
