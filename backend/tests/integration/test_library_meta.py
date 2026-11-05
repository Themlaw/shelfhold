"""unit.json is a projection of the database (ENF-OPS-020, ENF-OPS-013)."""

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.schema import catalog
from shelfhold.library.public import MetaProjection, PassthroughStore, meta_path
from tests.integration.test_schema_catalog import add_edition, add_work
from tests.library_env import LibEnv, cbz_bytes, lib_env
from tests.null_tombstones import NONE_PURGED


def ingest_one(env: LibEnv) -> None:
    PassthroughStore(env.engine, env.fsops, env.locate, env.data, tombstones=NONE_PURGED).ingest(
        unit_id=env.chain["unit"], variant_id=env.chain["variant"],
        staged=env.stage(cbz_bytes()), dest=str(env.manga / "S" / "S - 1.cbz"),
    )  # fmt: skip


def meta_files(env: LibEnv) -> list[Path]:
    return sorted(p for p in (env.data / "meta").rglob("*") if p.is_file())


@pytest.mark.req("ENF-OPS-020")
def test_meta_path_is_flat_and_by_ulid() -> None:
    unit = "01JC1234567890ABCDEFGHJKMN"
    assert meta_path(Path("/data"), unit) == Path(
        "/data/meta/units/01/01JC1234567890ABCDEFGHJKMN.json"
    )
    with pytest.raises(ValueError):
        meta_path(Path("/data"), "../../etc/passwd")


@pytest.mark.req("ENF-OPS-020")
def test_db_is_authority_json_rebuilt_after_crash() -> None:
    with lib_env() as env:
        ingest_one(env)
        (path,) = meta_files(env)
        good = path.read_bytes()
        projection = MetaProjection(env.engine, env.data)

        path.unlink()  # crash after the commit, before the file: the file is simply missing
        report = projection.repair()
        assert report.written == [env.chain["unit"]] and path.read_bytes() == good

        path.write_bytes(b'{"schema_version": "1.1", "unit_id": "x", "contents": []')  # torn write
        assert projection.repair().written == [env.chain["unit"]] and path.read_bytes() == good

        document = json.loads(good)  # drifted: the file says something the database does not
        document["contents"][0]["content_hash"] = "sha256:" + "0" * 64
        path.write_text(json.dumps(document), encoding="utf-8")
        assert projection.repair().written == [env.chain["unit"]] and path.read_bytes() == good

        assert projection.repair().written == []  # nothing to do: nothing is rewritten


@pytest.mark.req("ENF-OPS-020")
def test_a_crash_between_the_commit_and_the_projection_is_repaired_at_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with lib_env() as env:

        def die(self: MetaProjection, unit_id: str) -> None:
            raise KeyboardInterrupt("kill -9")

        monkeypatch.setattr(MetaProjection, "project", die)
        with pytest.raises(KeyboardInterrupt):
            ingest_one(env)
        monkeypatch.undo()
        assert meta_files(env) == []
        with env.engine.connect() as c:  # the transaction was validated: the database has it all
            assert c.execute(sa.text("SELECT count(*) FROM content")).scalar_one() == 1
        assert MetaProjection(env.engine, env.data).repair().written == [env.chain["unit"]]
        assert len(meta_files(env)) == 1


@pytest.mark.req("ENF-OPS-020")
def test_merge_moves_no_meta_file() -> None:
    with lib_env() as env:
        ingest_one(env)
        before = meta_files(env)
        with env.engine.begin() as c:  # a merge re-parents the Unit under another Edition
            work = add_work(c)
            c.execute(sa.update(catalog.unit).values(edition_id=add_edition(c, work)))
        report = MetaProjection(env.engine, env.data).repair()
        assert report.written == [env.chain["unit"]]  # the content follows the database...
        assert meta_files(env) == before  # ...in the very same file: nothing moved, nothing new
        assert json.loads(before[0].read_bytes())["edition_id"] != env.chain["edition"]
        assert all(env.chain["work"] not in str(p) for p in before)  # no hierarchy in the path


@pytest.mark.req("ENF-OPS-013")
def test_newer_format_on_disk_is_reported_and_left_untouched() -> None:
    with lib_env() as env:
        ingest_one(env)
        (path,) = meta_files(env)
        newer = json.loads(path.read_bytes()) | {"schema_version": "1.9", "x_new": 1}
        path.write_text(json.dumps(newer), encoding="utf-8")
        frozen = path.read_bytes()
        report = MetaProjection(env.engine, env.data).repair()
        assert report.read_only == [env.chain["unit"]] and report.written == []
        assert path.read_bytes() == frozen  # never rewritten, never "repaired"


@pytest.mark.req("ENF-OPS-013")
def test_older_minor_on_disk_is_migrated_when_next_written_and_unknown_fields_survive() -> None:
    with lib_env() as env:
        ingest_one(env)
        (path,) = meta_files(env)
        old = json.loads(path.read_bytes())
        old.update(schema_version="1.0", x_keep={"a": 1})
        old.pop("file_hashes")
        path.write_text(json.dumps(old), encoding="utf-8")
        assert MetaProjection(env.engine, env.data).repair().written == [env.chain["unit"]]
        new = json.loads(path.read_bytes())
        assert (
            new["schema_version"] == "1.1"
            and new["x_keep"] == {"a": 1}
            and len(new["file_hashes"]) == 1
        )
