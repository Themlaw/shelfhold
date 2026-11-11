"""Safeguard (EF-BIB-048, EF-BIB-045): the original goes to /store, never a rendered file."""

import hashlib
import io
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.packaging.cbz import repackage_cbz
from shelfhold.infrastructure.schema import storage
from shelfhold.library.public import (
    PassthroughStore,
    Received,
    Safeguard,
    SafeguardFailedError,
    content_hash,
    independent_copies,
)
from tests.library_env import (
    JPEG,
    OLD_COMICINFO,
    PNG,
    RENDERED_COMICINFO,
    LibEnv,
    cbz_bytes,
    lib_env,
)
from tests.locks_env import work_lock
from tests.null_tombstones import NONE_PURGED


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def hash_of(data: bytes) -> str:
    with safe_open_archive(io.BytesIO(data), name="x.cbz") as archive:
        return content_hash(archive)


def render(received: bytes) -> bytes:
    out = io.BytesIO()
    with safe_open_archive(io.BytesIO(received), name="x.cbz") as source:
        repackage_cbz(source, RENDERED_COMICINFO, out)
    return out.getvalue()


def safeguard(env: LibEnv) -> Safeguard:
    return Safeguard(env.engine, env.fsops, env.locate, str(env.store), env.data)


def ingest_and_render(env: LibEnv, received: bytes) -> Path:
    """Ingest as received, then let the exporter replace the file with its rendering."""
    dest = env.manga / "Dr Stone" / "Dr Stone - 1.cbz"
    PassthroughStore(
        env.engine, env.fsops, env.locate, env.data, tombstones=NONE_PURGED,
        hold_work=work_lock(env.engine),
    ).ingest(
        unit_id=env.chain["unit"], variant_id=env.chain["variant"],
        staged=env.stage(received), dest=str(dest),
    )  # fmt: skip
    rendered = render(received)
    env.fsops.write_atomic(str(dest), rendered)
    identity = dest.stat()  # replacing the file changed its inode: the registry records the new one
    with env.engine.begin() as c:  # the exporter's manifest: the file hash of this destination
        c.execute(
            sa.update(storage.replica).values(
                file_hash=sha(rendered),
                dev=identity.st_dev,
                ino=identity.st_ino,
                size=identity.st_size,
                mtime_ns=identity.st_mtime_ns,
            )
        )  # a stale inode could be reused by the next file written: two copies counted as one
    return dest


def upstream_of(env: LibEnv, data: bytes):
    @contextmanager
    def fetch() -> Iterator[Received]:
        path = env.stage(data)
        yield Received(path=path, size=len(data), sha256=sha(data))

    return fetch


def entries(path: Path) -> list[str]:
    with safe_open_archive(path) as archive:
        return [e.name for e in archive.entries]


@pytest.mark.req("EF-BIB-048")
def test_safeguard_from_library_strips_rendering_and_verifies_content_hash() -> None:
    received = cbz_bytes()
    with lib_env() as env:
        dest = ingest_and_render(env, received)
        assert "ComicInfo.xml" in entries(dest)  # the library file is the rendered one
        result = safeguard(env).run(env.chain["unit"])  # the source is gone: no upstream
        assert result.source == "library" and result.provenance == "extracted"
        (stored,) = env.files_under(env.store)
        assert "ComicInfo.xml" not in entries(stored)  # Shelfhold's rendering is stripped
        assert hash_of(stored.read_bytes()) == hash_of(received) == result.content_hash
        with env.engine.connect() as c:
            row = c.execute(sa.select(storage.content)).one()
            assert (row.retention, row.store_provenance) == ("keep", "extracted")
            assert row.path == stored.relative_to(env.store).as_posix()
            assert row.original_sha256 == sha(
                received
            )  # what was received is still what was received
            assert (
                independent_copies(c, env.chain["unit"]) == 2
            )  # library + a distinct inode in store
        inodes = {stored.stat().st_ino, dest.stat().st_ino}
        assert len(inodes) == 2  # never a hard link (EF-BIB-027)


@pytest.mark.req("EF-BIB-048")
def test_safeguard_refuses_and_writes_nothing_when_the_content_hash_does_not_verify() -> None:
    with lib_env() as env:
        dest = ingest_and_render(env, cbz_bytes())
        tampered = render(cbz_bytes((("001.png", PNG), ("002.jpg", JPEG + b"edited"))))
        env.fsops.write_atomic(str(dest), tampered)
        with env.engine.begin() as c:  # still the manifest's file hash: only the bytes changed
            c.execute(sa.update(storage.replica).values(file_hash=sha(tampered)))
        with pytest.raises(SafeguardFailedError) as raised:
            safeguard(env).run(
                env.chain["unit"]
            )  # no upstream, the library file is not the payload
        assert (
            raised.value.code == "unit.safeguard_failed"
            and raised.value.reason == "content_hash_mismatch"
        )
        assert env.files_under(env.store) == []
        with env.engine.connect() as c:
            assert c.execute(sa.select(storage.content.c.retention)).scalar_one() == "passthrough"
            assert (
                c.execute(
                    sa.select(sa.func.count())
                    .select_from(storage.replica)
                    .where(storage.replica.c.location == "store")
                ).scalar_one()
                == 0
            )


@pytest.mark.req("EF-BIB-045")
def test_safeguard_reads_upstream_when_library_file_differs_from_manifest() -> None:
    received = cbz_bytes(comicinfo=OLD_COMICINFO)
    with lib_env() as env:
        dest = ingest_and_render(env, received)
        third_party = render(cbz_bytes((("001.png", PNG), ("002.jpg", JPEG), ("003.png", PNG))))
        env.fsops.write_atomic(str(dest), third_party)  # someone replaced the file in /library
        result = safeguard(env).run(env.chain["unit"], upstream=upstream_of(env, received))
        assert result.source == "upstream" and result.provenance == "received"
        (stored,) = env.files_under(env.store)
        assert stored.read_bytes() == received  # the original as received, comic info included
        with env.engine.connect() as c:
            # the registry reflects the real state: the altered library file is not a copy of it
            assert independent_copies(c, env.chain["unit"]) == 1
            library = c.execute(
                sa.select(storage.replica).where(storage.replica.c.location == "library")
            ).one()
            assert library.content_hash == hash_of(third_party) and library.file_hash == sha(
                render(received)
            )


@pytest.mark.req("EF-BIB-045")
def test_safeguard_prefers_the_origin_when_both_are_intact() -> None:
    received = cbz_bytes()
    with lib_env() as env:
        ingest_and_render(env, received)
        result = safeguard(env).run(env.chain["unit"], upstream=upstream_of(env, received))
        assert result.source == "upstream"


@pytest.mark.req("EF-BIB-048")
def test_safeguard_refuses_an_upstream_that_is_another_revision() -> None:
    with lib_env() as env:
        ingest_and_render(env, cbz_bytes())
        newer = cbz_bytes((("001.png", PNG), ("002.jpg", JPEG + b"v2")))
        with pytest.raises(SafeguardFailedError):
            safeguard(env).run(env.chain["unit"], upstream=upstream_of(env, newer))
        assert env.files_under(env.store) == []


@pytest.mark.req("EF-BIB-048")
def test_store_never_holds_rendered_file() -> None:
    received = cbz_bytes()
    with lib_env() as env:
        dest = ingest_and_render(env, received)
        rendered = dest.read_bytes()
        safeguard(env).run(env.chain["unit"])
        for stored in env.files_under(env.store):
            assert stored.read_bytes() != rendered and "ComicInfo.xml" not in entries(stored)
    with lib_env() as env:  # and the writer itself refuses the bytes rendered for a destination
        dest = ingest_and_render(env, received)
        rendered = dest.read_bytes()
        with pytest.raises(SafeguardFailedError) as raised:
            safeguard(env).run(env.chain["unit"], upstream=upstream_of(env, rendered))
        assert raised.value.reason == "rendered_file"  # same content_hash, but not the original
        assert env.files_under(env.store) == []
