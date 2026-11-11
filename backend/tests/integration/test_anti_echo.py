"""M2 L22 (EF-SRC-006, I50): a file a source hands back whose `file_hash` or `content_hash` is one
of an export of Shelfhold's own becomes a replica of that `export_item`, never a Variant. The rule
holds for every source: the tests go through the one-shot acquisition and the connected-service
tickets. Each case tries to break it."""

from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.content_trust.epub_scan import epub_payload_hash
from shelfhold.library.domain.content_hash import content_hash
from shelfhold.sdk.testing.corpus.trapped_epubs.build import epub_bytes, xhtml
from tests.acquire_env import Bench, NativeBooks, acquire_bench
from tests.anti_echo_env import seed_own_export
from tests.library_env import OLD_COMICINFO, RENDERED_COMICINFO, cbz_bytes

CLEAN = epub_bytes(xhtml("<p>chapitre</p>"))
OTHER = epub_bytes(xhtml("<p>un autre chapitre</p>"))
FILE = "/files/book-42.epub"
CBZ_FILE = "/files/book-42.cbz"
CBZ_MIME = "application/vnd.comicbook+zip"


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def epub_payload(data: bytes) -> str:
    with safe_open_archive(io.BytesIO(data), name="x.epub") as archive:
        return epub_payload_hash(archive)


def cbz_payload(data: bytes) -> str:
    with safe_open_archive(io.BytesIO(data), name="x.cbz") as archive:
        return content_hash(archive)


def tree(bench: Bench) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("unit_variant", "unit", "content", "work", "replica"):
        (row,) = bench.rows(f"SELECT count(*) AS n FROM {table}")
        counts[table] = row["n"]
    return counts


def acquire(bench: Bench) -> Any:
    return bench.prepared_and_fetched()


def library_files(bench: Bench) -> list[Path]:
    return bench.lib.files_under(bench.lib.manga) + bench.lib.files_under(bench.lib.store)


def seeded_counts(bench: Bench) -> dict[str, int]:
    return tree(bench)


# -- the rule ---------------------------------------------------------------------------------


@pytest.mark.req("EF-SRC-006")
def test_opds_source_reimport_of_own_export_is_replica_not_variant() -> None:
    """The EPUB Shelfhold exported comes back from a source with the same bytes: it is a replica of
    the `export_item`, there is no Variant and no file kept (I50)."""
    with acquire_bench(NativeBooks(trust="publisher"), files={FILE: CLEAN}) as b:
        own = seed_own_export(b.lib, file_hash=sha(CLEAN))
        before = tree(b)
        prepared, outcome = acquire(b)
        assert outcome.state == "replica" and outcome.reason == "own_export"
        after = tree(b)
        assert after["unit_variant"] == before["unit_variant"] == 0  # never a Variant
        assert after["content"] == 0 and not library_files(b)  # nothing filed
        (replica,) = b.rows(
            "SELECT export_item_id, unit_id, location, file_hash, independent, remote_ref "
            "FROM replica"
        )
        assert replica["export_item_id"] == own.export_item_id
        assert replica["unit_id"] is None and replica["independent"] == 0
        assert replica["location"] == "upstream" and replica["file_hash"] == sha(CLEAN)
        assert prepared.request_id is not None
        (request,) = b.rows("SELECT state FROM download_request")
        assert request["state"] == "cancelled"  # closed: asking again would find the same file
        assert not b.delivered  # no `unit.stored`: nothing arrived


@pytest.mark.req("EF-SRC-006")
def test_anti_echo_matches_content_hash_when_a_server_rewrote_the_sidecar() -> None:
    """Kavita rewrote the ComicInfo of the CBZ we exported: the file hash differs, the payload
    (the images) does not. Still our export."""
    ours = cbz_bytes(comicinfo=RENDERED_COMICINFO)
    served = cbz_bytes(comicinfo=OLD_COMICINFO)
    assert sha(ours) != sha(served) and cbz_payload(ours) == cbz_payload(served)
    plugin = NativeBooks(trust="publisher", file_url="https://books.example.org" + CBZ_FILE,
                         mime=CBZ_MIME)  # fmt: skip
    with acquire_bench(plugin, files={CBZ_FILE: served}) as b:
        own = seed_own_export(b.lib, file_hash=sha(ours), content_hash=cbz_payload(ours))
        _prepared, outcome = acquire(b)
        assert outcome.state == "replica"
        (replica,) = b.rows("SELECT export_item_id, content_hash FROM replica")
        assert replica["export_item_id"] == own.export_item_id
        assert replica["content_hash"] == cbz_payload(served)
        assert tree(b)["unit_variant"] == 0 and tree(b)["content"] == 0


@pytest.mark.req("EF-SRC-006")
def test_anti_echo_matches_an_old_document_alias() -> None:
    """The file was rewritten since (an upgrade): the hash of an older rendering is still ours."""
    with acquire_bench(NativeBooks(trust="publisher"), files={FILE: CLEAN}) as b:
        own = seed_own_export(
            b.lib, file_hash=sha(OTHER), alias_file_hash=sha(CLEAN),
            alias_content_hash=epub_payload(CLEAN),
        )  # fmt: skip
        _prepared, outcome = acquire(b)
        assert outcome.state == "replica"
        (replica,) = b.rows("SELECT export_item_id FROM replica")
        assert replica["export_item_id"] == own.export_item_id
        assert tree(b)["unit_variant"] == 0


@pytest.mark.req("EF-SRC-006")
def test_anti_echo_matches_the_content_an_export_item_was_made_from() -> None:
    """The payload of the `content` behind an `export_item`, original or derived."""
    for column in ("kept_content_hash", "kept_derived_hash"):
        with acquire_bench(NativeBooks(trust="publisher"), files={FILE: CLEAN}) as b:
            own = seed_own_export(b.lib, **{column: epub_payload(CLEAN)})
            _prepared, outcome = acquire(b)
            assert outcome.state == "replica", column
            (replica,) = b.rows("SELECT export_item_id FROM replica")
            assert replica["export_item_id"] == own.export_item_id
            assert tree(b)["unit_variant"] == 0


@pytest.mark.req("EF-SRC-006")
def test_anti_echo_does_not_touch_a_file_that_is_not_ours() -> None:
    """Control: an export of something else, a file that matches nothing, is filed as always."""
    with acquire_bench(NativeBooks(trust="publisher"), files={FILE: CLEAN}) as b:
        seed_own_export(b.lib, file_hash=sha(OTHER), content_hash=epub_payload(OTHER))
        _prepared, outcome = acquire(b)
        assert outcome.state == "fetched"
        assert tree(b)["unit_variant"] == 1 and tree(b)["content"] == 1
        assert b.rows("SELECT id FROM replica WHERE export_item_id IS NOT NULL") == []


@pytest.mark.req("EF-SRC-006")
def test_anti_echo_never_matches_an_absent_hash() -> None:
    """Attack: an export with no hash recorded yet (NULL, planned) must not match every file."""
    with acquire_bench(NativeBooks(trust="publisher"), files={FILE: CLEAN}) as b:
        seed_own_export(b.lib, file_hash=None, content_hash=None)
        _prepared, outcome = acquire(b)
        assert outcome.state == "fetched" and tree(b)["unit_variant"] == 1


@pytest.mark.req("EF-SRC-006")
def test_anti_echo_applies_whatever_the_trust_of_the_source() -> None:
    """Attack: an `untrusted` source cannot make its echo a Variant; the rule is not a gift of
    the manifest. The file is not neutralized and kept as a copy either: it is ours already."""
    with acquire_bench(NativeBooks(trust="untrusted"), files={FILE: CLEAN}) as b:
        seed_own_export(b.lib, file_hash=sha(CLEAN))
        _prepared, outcome = acquire(b)
        assert outcome.state == "replica"
        assert tree(b)["content"] == 0 and not library_files(b)


@pytest.mark.req("EF-SRC-006")
def test_anti_echo_replay_records_one_replica() -> None:
    """The same echo met twice (a retry, two pages of a feed) is one replica of the export."""
    with acquire_bench(NativeBooks(trust="publisher"), files={FILE: CLEAN}) as b:
        seed_own_export(b.lib, file_hash=sha(CLEAN))
        acquire(b)
        acquire(b)
        assert tree(b)["replica"] == 1 and tree(b)["unit_variant"] == 0


@pytest.mark.req("EF-SRC-006")
def test_anti_echo_leaves_nothing_in_staging(tmp_path: Path) -> None:
    with acquire_bench(NativeBooks(trust="publisher"), files={FILE: CLEAN}) as b:
        seed_own_export(b.lib, file_hash=sha(CLEAN))
        acquire(b)
        assert list((b.lib.tmp / "recv").iterdir()) == []


@pytest.mark.req("EF-SRC-006")
def test_anti_echo_does_not_count_the_replica_as_an_independent_copy() -> None:
    """A replica of a destination's file is not a copy we hold (EF-BIB-045): the purge and the
    safeguard must not take it for a safe one."""
    from shelfhold.library.public import independent_copies

    with acquire_bench(NativeBooks(trust="publisher"), files={FILE: CLEAN}) as b:
        seed_own_export(b.lib, file_hash=sha(CLEAN))
        prepared, _outcome = acquire(b)
        with b.lib.engine.connect() as connection:
            assert independent_copies(connection, prepared.unit_id) == 0


@pytest.mark.req("EF-SRC-006")
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    by=st.sampled_from(["file_hash", "content_hash", "alias", "kept", "derived"]),
    trust=st.sampled_from(["publisher", "untrusted"]),
)
def test_reimport_never_creates_a_variant_property(by: str, trust: str) -> None:
    """Property: whichever hash the echo matches, whatever the source's trust, no Variant is made."""
    payload = epub_payload(CLEAN)
    seeds: dict[str, dict[str, Any]] = {
        "file_hash": {"file_hash": sha(CLEAN)},
        "content_hash": {"file_hash": sha(OTHER), "content_hash": payload},
        "alias": {"alias_file_hash": sha(CLEAN), "alias_content_hash": payload},
        "kept": {"kept_content_hash": payload},
        "derived": {"kept_derived_hash": payload},
    }
    with acquire_bench(NativeBooks(trust=trust), files={FILE: CLEAN}) as b:
        seed_own_export(b.lib, **seeds[by])
        _prepared, outcome = acquire(b)
        assert outcome.state == "replica"
        assert tree(b)["unit_variant"] == 0 and tree(b)["content"] == 0


def zip_with(name: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, b"x")
    return buffer.getvalue()


@pytest.mark.req("EF-SRC-006", "ENF-SEC-015")
def test_anti_echo_runs_after_the_archive_audit() -> None:
    """Attack: a hostile archive is not read for its content hash outside the audit: it is
    refused as before (and never recorded as a replica of anything)."""
    hostile = zip_with("../evil.txt")
    with acquire_bench(NativeBooks(trust="publisher"), files={FILE: hostile}) as b:
        seed_own_export(b.lib, file_hash=sha(CLEAN))
        _prepared, outcome = acquire(b)
        assert outcome.state == "abandoned"
        assert tree(b)["replica"] == 0


def run(coro: Any) -> Any:
    return asyncio.run(coro)
