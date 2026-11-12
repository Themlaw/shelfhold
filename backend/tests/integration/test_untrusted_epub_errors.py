"""M2 L03: refusals and error paths of the derivative pipeline (EF-BIB-051).

Everything here is a behaviour an admin can meet: a purged file that comes back, a file too
large, a file that is not what the download counted, a second arrival, a destination file that is
not the derivative, a library file that is gone or that the neutralizer would still change.
"""

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.locks import work_lock_for
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.content_trust import find_active_content
from shelfhold.infrastructure.schema import replica as replica_schema
from shelfhold.infrastructure.schema import storage
from shelfhold.library.public import (
    OriginalNotServableError,
    ReceivedTooLargeError,
    RegenerationBlockedError,
    TombstonedContentError,
    UntrustedEpubStore,
    VariantAlreadyStoredError,
    has_active_content,
    has_epub,
    require_servable_derivative,
    servable_epub,
)
from shelfhold.sdk.testing.corpus.trapped_epubs.build import Member, epub_bytes, xhtml
from tests.integration.test_untrusted_epub_pipeline import (
    CLEAN,
    TRAPPED,
    content_row,
    file_in,
    ingest,
    set_binding_trust,
    sha,
    store,
)
from tests.library_env import lib_env

pytestmark = pytest.mark.req("EF-BIB-051")


class Purged:
    def check_content(self, hashes: object = (), **_: Any) -> bool:
        return True


def test_a_purged_content_is_never_filed_again() -> None:
    with lib_env() as env:
        dest, staged = file_in(env, TRAPPED)
        with pytest.raises(TombstonedContentError):
            store(env, tombstones=Purged()).ingest(
                unit_id=env.chain["unit"],
                variant_id=env.chain["variant"],
                staged=staged,
                dest=dest,
            )
        assert not Path(dest).exists() and env.files_under(env.store) == []


def test_a_purged_publisher_epub_is_not_filed_either() -> None:
    with lib_env() as env:
        set_binding_trust(env, "publisher")
        dest, staged = file_in(env, CLEAN)
        with pytest.raises(TombstonedContentError):
            store(env, tombstones=Purged()).ingest(
                unit_id=env.chain["unit"],
                variant_id=env.chain["variant"],
                staged=staged,
                dest=dest,
            )


def test_a_file_above_the_bound_is_refused_before_it_is_opened() -> None:
    with lib_env() as env:
        dest, staged = file_in(env, TRAPPED)
        small = UntrustedEpubStore(
            env.engine, env.fsops, env.locate, env.data, store_dir=str(env.store),
            tombstones=Purged(), max_received_bytes=10,
            hold_work=work_lock_for(env.engine, clock=SystemClock()),
        )  # fmt: skip
        with pytest.raises(ReceivedTooLargeError):
            small.ingest(
                unit_id=env.chain["unit"],
                variant_id=env.chain["variant"],
                staged=staged,
                dest=dest,
            )


def test_a_file_that_is_not_what_the_download_counted_is_refused() -> None:
    with lib_env() as env:
        dest, staged = file_in(env, TRAPPED)
        with pytest.raises(ValueError, match="received_hash_mismatch"):
            store(env).ingest(
                unit_id=env.chain["unit"],
                variant_id=env.chain["variant"],
                staged=staged,
                dest=dest,
                expected_sha256=sha(b"other"),
            )


def test_the_same_release_twice_is_a_no_op_error() -> None:
    with lib_env() as env:
        ingest(env)
        dest, staged = file_in(env, TRAPPED, "Again.epub")
        with pytest.raises(VariantAlreadyStoredError):
            store(env).ingest(
                unit_id=env.chain["unit"],
                variant_id=env.chain["variant"],
                staged=staged,
                dest=dest,
            )


def test_the_origin_is_recorded_as_a_copy_that_is_not_ours() -> None:
    with lib_env() as env:
        dest, staged = file_in(env, TRAPPED)
        store(env).ingest(
            unit_id=env.chain["unit"],
            variant_id=env.chain["variant"],
            staged=staged,
            dest=dest,
            upstream_ref="remote-1",
        )
        with env.engine.connect() as c:
            places = {r.location for r in c.execute(sa.select(replica_schema.replica))}
        assert places == {"library", "store", "upstream"}


def test_a_publisher_epub_records_its_origin_too() -> None:
    with lib_env() as env:
        set_binding_trust(env, "publisher")
        dest, staged = file_in(env, CLEAN)
        store(env).ingest(
            unit_id=env.chain["unit"],
            variant_id=env.chain["variant"],
            staged=staged,
            dest=dest,
            upstream_ref="remote-1",
        )
        with env.engine.connect() as c:
            assert {r.location for r in c.execute(sa.select(replica_schema.replica))} == {
                "library", "upstream",
            }  # fmt: skip


def test_a_destination_file_that_is_not_clean_is_not_taken_back() -> None:
    """Same payload hash is not enough: the neutralizer must have nothing left to change."""
    with lib_env() as env:
        dest, staged = file_in(env, TRAPPED)
        from shelfhold.infrastructure.archives import safe_open_archive
        from shelfhold.infrastructure.content_trust import epub_payload_hash

        with safe_open_archive(staged, name="x.epub") as archive:
            payload = epub_payload_hash(archive)
        with pytest.raises(OriginalNotServableError) as refused:
            store(env).restore_from_destination(
                unit_id=env.chain["unit"],
                variant_id=env.chain["variant"],
                found=staged,
                dest=dest,
                expected_derived_hash=payload,
                derived_by="xhtml-neutralizer@v1",
            )
        assert refused.value.reason == "not_clean"


def test_nothing_is_servable_for_a_unit_without_an_epub() -> None:
    with lib_env() as env:
        with env.engine.connect() as c:
            assert not has_epub(c, env.chain["unit"])
            require_servable_derivative(c, env.chain["unit"])  # not concerned: no error
        with pytest.raises(OriginalNotServableError) as refused:
            servable_epub(env.engine, env.fsops, env.chain["unit"])
        assert refused.value.reason == "no_epub"


def test_an_adopted_in_place_epub_is_never_served() -> None:
    with lib_env() as env:
        ingest(env)
        with env.engine.begin() as c:
            c.execute(sa.update(storage.content).values(retention="adopted_ref", path=None))
        with pytest.raises(OriginalNotServableError) as refused:
            servable_epub(env.engine, env.fsops, env.chain["unit"])
        assert refused.value.reason == "adopted_in_place"


def test_the_database_guard_refuses_an_untrusted_epub_without_derivative() -> None:
    with lib_env() as env:
        ingest(env)
        with env.engine.begin() as c:
            c.execute(sa.update(storage.content).values(
                derived_hash=None, derived_by=None, derived_hash_by=None))  # fmt: skip
        with env.engine.connect() as c, pytest.raises(OriginalNotServableError):
            require_servable_derivative(c, env.chain["unit"])


def test_a_vanished_library_file_serves_nothing() -> None:
    with lib_env() as env:
        _result, library = ingest(env)
        library.unlink()
        with pytest.raises(OriginalNotServableError):
            servable_epub(env.engine, env.fsops, env.chain["unit"])


def test_regeneration_of_a_unit_with_no_derivative_is_blocked() -> None:
    with lib_env() as env, pytest.raises(RegenerationBlockedError):
        store(env).regenerate(env.chain["unit"])


def test_regeneration_refuses_a_release_that_is_not_the_target() -> None:
    with lib_env() as env:
        _result, library = ingest(env)
        before = library.read_bytes()
        with pytest.raises(RegenerationBlockedError) as refused:
            store(env).regenerate(env.chain["unit"], target="xhtml-neutralizer@v9")
        assert refused.value.reason == "derived_by_is_not_the_target"
        assert library.read_bytes() == before  # nothing replaced


def test_regeneration_without_a_library_copy_is_blocked() -> None:
    with lib_env() as env:
        _result, library = ingest(env)
        with env.engine.begin() as c:
            c.execute(
                sa.delete(replica_schema.replica).where(
                    replica_schema.replica.c.location == "library"
                )
            )
        with pytest.raises(RegenerationBlockedError):
            store(env).regenerate(env.chain["unit"])
        assert library.exists()


def test_an_unreadable_adopted_file_is_not_reported() -> None:
    with lib_env() as env:
        assert has_active_content(env.fsops, str(env.manga / "missing.epub")) is False


# -- find_active_content: what the adoption report reads -----------------------------------------


@pytest.mark.parametrize(
    ("chapter", "extra"),
    [
        ('<p><a href="javascript:alert(1)">x</a></p>', []),
        ('<p onclick="x()">x</p>', []),
        ('<img src="http://evil.example/x.png" alt=""/>', []),
        ('<iframe src="a.xhtml"></iframe>', []),
        ("<p>ok</p>", [Member("OEBPS/extra.css", b"@import url(http://evil.example/a.css);")]),
    ],
)
def test_active_content_is_found(chapter: str, extra: list[Member]) -> None:
    import io

    data = epub_bytes(xhtml(chapter), extra=extra)
    assert find_active_content(io.BytesIO(data))


def test_clean_pages_and_local_references_are_not_active() -> None:
    import io

    data = epub_bytes(
        xhtml('<p><a href="http://example.org">link</a></p><img src="cover.png" alt=""/>'),
        extra=[Member("OEBPS/extra.css", b"p { color: red; } @font-face { src: url(f.woff2); }")],
    )
    assert find_active_content(io.BytesIO(data)) == ()


def test_a_page_the_reader_refuses_and_a_non_archive_are_reported() -> None:
    import io

    broken = epub_bytes(xhtml("<p>x</p>"), extra=[Member("OEBPS/bad.xhtml", b"<html><p></html>")])
    assert find_active_content(io.BytesIO(broken)) == ("OEBPS/bad.xhtml",)
    assert find_active_content(io.BytesIO(b"not an archive"), name="x.epub") == ("x.epub",)


def test_unused_content_row_is_untouched_by_a_failed_ingest() -> None:
    with lib_env() as env:
        ingest(env)
        before = content_row(env)
        dest, staged = file_in(env, TRAPPED, "Again.epub")
        with pytest.raises(VariantAlreadyStoredError):
            store(env).ingest(
                unit_id=env.chain["unit"],
                variant_id=env.chain["variant"],
                staged=staged,
                dest=dest,
            )
        assert content_row(env) == before


# -- the Work lock: a purge and a write of the same Work never interleave (EF-BIB-052) ------------


def _purge_holding(env: Any) -> Any:
    """What `purge --now` does first: it holds the lock of the Work it is about to erase."""
    from shelfhold.export.application.locks import ResourceLocks, work_resource
    from shelfhold.infrastructure.ids import new_ulid

    return ResourceLocks(env.engine, clock=SystemClock()).hold_resources(
        new_ulid(), [work_resource(env.chain["work"])]
    )


def test_ingest_waits_for_a_purge_of_the_same_work_and_writes_nothing() -> None:
    from shelfhold.export.application.locks import LockBusyError

    with lib_env() as env:
        dest, staged = file_in(env, TRAPPED)
        with _purge_holding(env), pytest.raises(LockBusyError):
            store(env).ingest(
                unit_id=env.chain["unit"],
                variant_id=env.chain["variant"],
                staged=staged,
                dest=dest,
            )
        assert not Path(dest).exists() and env.files_under(env.store) == []  # no orphan copy
        with env.engine.connect() as c:
            assert (
                c.execute(sa.select(sa.func.count()).select_from(storage.content)).scalar_one() == 0
            )
        ingest(env)  # the purge is over: the same ingest goes through
        assert len(env.files_under(env.store)) == 1


def test_regenerate_waits_for_a_purge_of_the_same_work_and_replaces_nothing() -> None:
    from shelfhold.export.application.locks import LockBusyError

    with lib_env() as env:
        _result, library = ingest(env)
        before = (library.read_bytes(), library.stat().st_ino)
        with _purge_holding(env), pytest.raises(LockBusyError):
            store(env).regenerate(env.chain["unit"])
        assert (library.read_bytes(), library.stat().st_ino) == before
        store(env).regenerate(env.chain["unit"])  # after the purge's lock is released


def test_restore_from_destination_waits_for_a_purge_too() -> None:
    from shelfhold.export.application.locks import LockBusyError

    with lib_env() as env:
        dest, staged = file_in(env, CLEAN)
        with _purge_holding(env), pytest.raises(LockBusyError):
            store(env).restore_from_destination(
                unit_id=env.chain["unit"],
                variant_id=env.chain["variant"],
                found=staged,
                dest=dest,
                expected_derived_hash=sha(b"x"),
                derived_by="xhtml-neutralizer@v1",
            )
        assert not Path(dest).exists()


def test_the_store_cannot_be_built_without_the_work_lock() -> None:
    with lib_env() as env, pytest.raises(TypeError):
        UntrustedEpubStore(  # type: ignore[call-arg]
            env.engine,
            env.fsops,
            env.locate,
            env.data,
            store_dir=str(env.store),
            tombstones=Purged(),
        )


def test_a_regeneration_of_an_unknown_unit_is_blocked() -> None:
    with lib_env() as env, pytest.raises(RegenerationBlockedError):
        store(env).regenerate("0" * 26)
