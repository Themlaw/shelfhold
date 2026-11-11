"""Regression tests for the Opus review of PR #44 (each one was a working proof of concept)."""

import asyncio
import json
from pathlib import Path

import httpx2
import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.fetcher import FetchTooLarge, GuardedFetcher
from shelfhold.infrastructure.schema import catalog, storage
from shelfhold.library.public import (
    MetaProjection,
    PassthroughStore,
    Safeguard,
    SafeguardFailedError,
    UnitJson,
    UnitJsonCorruptError,
    irreplaceability,
    meta_path,
    receive_cbz,
)
from tests.fakes_fetcher import FakeResolver, Recorder
from tests.library_env import JPEG, OLD_COMICINFO, PNG, LibEnv, cbz_bytes, lib_env
from tests.locks_env import work_lock
from tests.null_tombstones import NONE_PURGED


def ingest(env: LibEnv, data: bytes, upstream_ref: str | None = None) -> Path:
    dest = env.manga / "S" / "S - 1.cbz"
    PassthroughStore(
        env.engine, env.fsops, env.locate, env.data, tombstones=NONE_PURGED,
        hold_work=work_lock(env.engine),
    ).ingest(
        unit_id=env.chain["unit"], variant_id=env.chain["variant"],
        staged=env.stage(data), dest=str(dest), upstream_ref=upstream_ref,
    )  # fmt: skip
    return dest


def guard(env: LibEnv) -> Safeguard:
    return Safeguard(env.engine, env.fsops, env.locate, str(env.store), env.data)


@pytest.mark.req("EF-BIB-045", "EF-BIB-048")
def test_a_third_party_file_never_becomes_a_manifest_source_on_the_second_pass() -> None:
    with lib_env() as env:
        dest = ingest(env, cbz_bytes(comicinfo=OLD_COMICINFO))
        dest.write_bytes(cbz_bytes(comicinfo=b"<ComicInfo><Series>3rd</Series></ComicInfo>"))
        for _ in range(3):  # the same altered file, untouched since: refused every time
            with pytest.raises(SafeguardFailedError, match="library_differs_from_manifest"):
                guard(env).run(env.chain["unit"])
        assert env.files_under(env.store) == []
        with env.engine.connect() as c:
            row = c.execute(sa.text("SELECT file_hash FROM replica")).scalar_one()
        assert (
            row
            == "sha256:"
            + __import__("hashlib").sha256(cbz_bytes(comicinfo=OLD_COMICINFO)).hexdigest()
        )  # the manifest still says what the exporter wrote


@pytest.mark.req("EF-BIB-045")
def test_a_changed_payload_fails_closed_on_every_pass() -> None:
    with lib_env() as env:
        dest = ingest(env, cbz_bytes())
        dest.write_bytes(cbz_bytes(pages=(("001.png", PNG + b"!"), ("002.jpg", JPEG))))
        for _ in range(2):
            with pytest.raises(SafeguardFailedError):
                guard(env).run(env.chain["unit"])
        assert env.files_under(env.store) == []


@pytest.mark.req("EF-BIB-045", "EF-BIB-031")
def test_a_dead_upstream_is_not_a_copy() -> None:
    with lib_env() as env:
        dest = ingest(env, cbz_bytes(), upstream_ref="suwayomi:chapter/42")
        with env.engine.connect() as c:
            assert irreplaceability(c, env.chain["unit"]).independent_copies == 2
        with env.engine.begin() as c:  # the source drops the chapter...
            c.execute(
                sa.update(catalog.unit_variant).values(removed_at=sa.func.current_timestamp())
            )
        with env.engine.connect() as c:
            assert irreplaceability(c, env.chain["unit"]).independent_copies == 1
        with env.engine.begin() as c:
            c.execute(sa.update(catalog.unit).values(availability="removed"))
        dest.unlink()  # ...and the library file goes too
        with pytest.raises(SafeguardFailedError):
            guard(env).run(env.chain["unit"])
        with env.engine.connect() as c:
            state = irreplaceability(c, env.chain["unit"])
        assert state.irreplaceable and state.independent_copies == 0


@pytest.mark.req("ENF-OPS-013")
def test_a_newer_minor_is_never_rewritten_whatever_it_contains() -> None:
    with lib_env() as env:
        ingest(env, cbz_bytes())
        path = meta_path(env.data, env.chain["unit"])
        doc = json.loads(path.read_bytes())
        doc["schema_version"] = "1.2"
        doc["file_hashes"] = [{"file_hash": "blake3:" + "a" * 64, "future": True}]
        raw = json.dumps(doc).encode()
        path.write_bytes(raw)
        assert MetaProjection(env.engine, env.data).project(env.chain["unit"]) == "read_only"
        assert path.read_bytes() == raw
    assert UnitJson.parse(raw).read_only  # parsing it is fine too


@pytest.mark.req("ENF-OPS-013")
def test_unit_json_hostile_shapes_are_corrupt_not_crashes() -> None:
    for raw in (
        b'{"schema_version":"9.0","schema_version":"1.1","unit_id":"a"}',  # hidden marker
        b'{"schema_version":"1.1","x":' + b"[" * 200_000 + b"]" * 200_000 + b"}",
        b'{"schema_version":"1.1","pad":"' + b"a" * (2 << 20) + b'"}',
    ):
        with pytest.raises(UnitJsonCorruptError):
            UnitJson.parse(raw)


@pytest.mark.req("ENF-OPS-020")
def test_repair_survives_a_nested_bomb_and_a_link_and_replaces_them() -> None:
    with lib_env() as env:
        ingest(env, cbz_bytes())
        path = meta_path(env.data, env.chain["unit"])
        good = path.read_bytes()
        path.write_bytes(b'{"schema_version":"1.1","x":' + b"[" * 200_000 + b"]" * 200_000 + b"}")
        assert MetaProjection(env.engine, env.data).repair().written == [env.chain["unit"]]
        assert path.read_bytes() == good
        big = env.tmp / "big"
        big.write_bytes(b" " * (64 << 20))
        path.unlink()
        path.symlink_to(big)  # never read through the link, never bounded by luck
        assert MetaProjection(env.engine, env.data).project(env.chain["unit"]) == "written"
        assert (
            not path.is_symlink() and path.read_bytes() == good and big.stat().st_size == 64 << 20
        )


@pytest.mark.req("ENF-SEC-002")
def test_a_download_over_the_bound_leaves_no_partial_file(tmp_path: Path) -> None:
    fetcher = GuardedFetcher(
        FakeResolver({"f.example.org": ["93.184.216.34"]}),
        transport=Recorder(lambda r: httpx2.Response(200, content=b"x" * (3 << 20))).transport,
    )

    async def go() -> None:
        async with receive_cbz(
            fetcher, "http://f.example.org/a.cbz", staging_dir=tmp_path, max_bytes=1 << 20
        ):
            pass

    with pytest.raises(FetchTooLarge):
        asyncio.run(go())
    assert list(tmp_path.iterdir()) == []
    assert storage.content is not None
