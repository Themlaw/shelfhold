"""The per-root provenance mirror, written under intent after the file (EF-EXP-063, I33)."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.export_items import add_coverage, create_export_item
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.manifest_write import adopt_written_hook
from shelfhold.export.application.mirror import ProvenanceMirror
from shelfhold.export.domain.physical_roots import RESERVED_DIR
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, coherence, export, platform
from tests.export_env import ExportEnv, export_env
from tests.integration.test_schema_catalog import add_unit, add_variant
from tests.mirror_env import entry, h, header, raw

pytestmark = pytest.mark.req("EF-EXP-063")


def mirror_files(env: ExportEnv) -> list[Path]:
    return sorted((env.lib.library / RESERVED_DIR / "manifest").rglob("*.jsonl"))


def lines_of(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def mirror_of(env: ExportEnv) -> ProvenanceMirror:
    return env.pipeline.mirror


@pytest.mark.req("EF-EXP-063", "EF-EXP-041")
def test_manifest_mirror_written_under_intent_after_file(monkeypatch: pytest.MonkeyPatch) -> None:
    with export_env() as env:
        order: list[str] = []
        states: list[str] = []
        real_atomic, real_mirror = Fsops.write_atomic, Fsops.write_mirror

        def atomic(self: Fsops, local: str, *args: Any, **kwargs: Any) -> Any:
            order.append("file")
            return real_atomic(self, local, *args, **kwargs)

        def mirror(self: Fsops, root_path: str, name: str, data: bytes) -> Any:
            order.append("mirror")
            with env.engine.connect() as connection:  # the intent is committed before the write
                states.extend(
                    connection.execute(
                        sa.select(coherence.fs_intent.c.state).where(
                            coherence.fs_intent.c.op == "write",
                            coherence.fs_intent.c.dst_path.like("%.jsonl"),
                        )
                    ).scalars()
                )
            return real_mirror(self, root_path, name, data)

        monkeypatch.setattr(Fsops, "write_atomic", atomic)
        monkeypatch.setattr(Fsops, "write_mirror", mirror)
        series = env.new_series()
        env.add_chapter(series, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert order == ["file", "mirror"]
        assert states == ["applying"]
        with env.engine.connect() as connection:
            rows = connection.execute(
                sa.select(coherence.fs_intent).where(coherence.fs_intent.c.dst_path.like("%.jsonl"))
            ).all()
        assert [(r.op, r.state) for r in rows] == [("write", "done")]
        (path,) = mirror_files(env)
        (record,) = env.rows(export.export_record)
        (header_line, line) = lines_of(path)
        assert header_line["schema_version"] == "1.2" and header_line["kind"] == "header"
        assert (line["rel_path"], line["file_hash"]) == (record.rel_path, record.file_hash)
        assert line["export_item"]["id"] == record.export_item_id
        assert line["content_hash"] == record.content_hash and line["state"] == "exported"


@pytest.mark.req("EF-EXP-063", "EF-EXP-041")
def test_mirror_intent_without_file_is_abandoned_at_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash between the intent and the write: the projection is rebuilt, nothing drifts."""

    class Crash(BaseException):
        pass

    with export_env() as env:

        def crash(self: Fsops, root_path: str, name: str, data: bytes) -> None:
            raise Crash

        monkeypatch.setattr(Fsops, "write_mirror", crash)
        series = env.new_series()
        env.add_chapter(series, "1")
        with pytest.raises(Crash):
            env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert len(env.files()) == 1 and mirror_files(env) == []  # the file is there, no line yet
        monkeypatch.undo()
        journal = IntentJournal(env.engine, env.lib.fsops, clock=env.clock)
        report = journal.resolve_open(
            on_done=adopt_written_hook(env.lib.fsops, env.lib.locate, env.clock)
        )
        assert report.still_open == [] and len(report.abandoned) == 1
        assert mirror_of(env).rebuild(env.destination_id) != []
        assert len(mirror_files(env)) == 1


@pytest.mark.req("EF-EXP-063", "EF-EXP-058")
def test_manifest_mirror_never_written_to_external_egress() -> None:
    with export_env() as env:
        with env.engine.begin() as connection:
            connection.execute(sa.update(export.shelf_destination).values(egress="externe"))
        series = env.new_series()
        env.add_chapter(series, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert len(env.files()) == 1  # the export itself goes ahead
        assert not (env.lib.library / RESERVED_DIR / "manifest").exists()
        assert mirror_of(env).rebuild(env.destination_id) == []  # nor on a rebuild
        assert mirror_files(env) == []


def test_manifest_mirror_rebuilt_from_db() -> None:
    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        env.add_chapter(series, "2")
        with env.engine.begin() as connection:  # a URL on the source, which must stay out
            connection.execute(sa.text("UPDATE source_item SET url = 'https://src.example/a/1'"))
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        (path,) = mirror_files(env)
        before = path.read_bytes()
        shutil.rmtree(env.lib.library / RESERVED_DIR / "manifest")
        written = mirror_of(env).rebuild(env.destination_id)
        assert [Path(p) for p in written] == [path]
        assert path.read_bytes() == before  # the database is the authority: same projection
        text = before.decode()
        assert "src.example" not in text and "http" not in text
        entries = lines_of(path)[1:]
        assert len(entries) == 2
        for line in entries:
            (covered,) = line["coverage"]
            assert covered["source_key"] == "suwayomi:1" and covered["remote_unit_id"]


def test_manifest_mirror_lineage_per_coverage_entry_from_the_database() -> None:
    """An adopted file covering 3 chapters of two teams (EF-BIB-044)."""
    with export_env() as env:
        series = env.new_series()
        with env.engine.begin() as connection:
            teams = []
            for label in ("alpha", "beta"):
                team_id = new_ulid()
                connection.execute(
                    sa.insert(catalog.team).values(
                        id=team_id, edition_id=series.edition_id, label=label
                    )
                )
                teams.append(team_id)
            units, variants = [], []
            for number in ("1", "2", "3"):
                unit = add_unit(connection, series.edition_id, number)
                variant = add_variant(connection, unit, series.binding_id, f"r-{number}")
                connection.execute(
                    sa.update(catalog.unit_variant)
                    .where(catalog.unit_variant.c.id == variant)
                    .values(team_id=teams[int(number) % 2])
                )
                units.append(unit)
                variants.append(variant)
            item = create_export_item(
                connection, edition_id=series.edition_id, unit_id=units[0], kind="adopted",
                frozen_attrs={"series": "Dr Stone"},
            )  # fmt: skip
            for unit in units[1:]:
                add_coverage(connection, item, unit)
            series_id = new_ulid()
            connection.execute(
                sa.insert(export.export_series).values(
                    id=series_id, shelf_destination_id=env.destination_id,
                    remote_library_id=env.remote_library_id, normalized_name="dr stone",
                    name_raw="Dr Stone", series_dir="Dr Stone",
                )
            )  # fmt: skip
            connection.execute(
                sa.insert(export.edition_export_series).values(
                    edition_id=series.edition_id, remote_library_id=env.remote_library_id,
                    export_series_id=series_id, assigned_by="adoption",
                    assigned_at=env.clock.now(),
                )
            )  # fmt: skip
            connection.execute(
                sa.insert(export.export_record).values(
                    shelf_destination_id=env.destination_id, export_item_id=item,
                    locator_type="path", rel_path="Dr Stone/Dr Stone - Vol.1.cbz",
                    content_hash=h("c"), file_hash=h("f"), state="adopted",
                )
            )  # fmt: skip
        (env.root / "Dr Stone").mkdir()
        (env.root / "Dr Stone" / "Dr Stone - Vol.1.cbz").write_bytes(b"x")
        mirror_of(env).rebuild(env.destination_id)
        (path,) = mirror_files(env)
        (line,) = lines_of(path)[1:]
        got = {c["unit"]: c for c in line["coverage"]}
        assert set(got) == set(units)
        assert [got[u]["variant"] for u in units] == variants
        assert [got[u]["team"]["label"] for u in units] == ["beta", "alpha", "beta"]
        assert [got[u]["remote_unit_id"] for u in units] == ["r-1", "r-2", "r-3"]
        assert line["state"] == "adopted" or line["state"] == "exported"


def test_unresolved_mirror_line_survives_a_rebuild() -> None:
    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        (path,) = mirror_files(env)
        head = lines_of(path)[0]
        stranger = entry("Dr Stone/born-after-backup.cbz", state="unresolved")
        path.write_bytes(path.read_bytes() + raw(stranger))
        for _ in range(2):  # `meta rebuild`, twice
            mirror_of(env).rebuild(env.destination_id)
            kept = {line["rel_path"]: line for line in lines_of(path)[1:]}
            assert kept["Dr Stone/born-after-backup.cbz"]["state"] == "unresolved"
            assert kept["Dr Stone/born-after-backup.cbz"]["export_item"] == stranger["export_item"]
            assert len(kept) == 2
        assert lines_of(path)[0]["export_series"] == head["export_series"]


def test_a_newer_minor_file_keeps_its_unknown_fields_through_a_rewrite() -> None:
    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        (path,) = mirror_files(env)
        head, line = lines_of(path)
        head.update(schema_version="1.9", planned_for="later")
        line["future"] = {"x": 1}
        path.write_bytes(raw(head, line))
        env.add_chapter(series, "2")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        head2, *lines = lines_of(path)
        assert head2["schema_version"] == "1.9" and head2["planned_for"] == "later"
        assert sorted(bool(x.get("future")) for x in lines) == [False, True]


def test_a_newer_major_file_is_left_alone() -> None:
    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        (path,) = mirror_files(env)
        frozen = raw(header("2.0"), entry())
        path.write_bytes(frozen)
        env.add_chapter(series, "2")
        env.pipeline.run(env.destination_id, job_id=new_ulid())  # the export itself goes on
        assert path.read_bytes() == frozen
        assert len(env.files()) == 2


def test_no_mirror_on_adopt_ro_root() -> None:
    with export_env() as env:
        with env.engine.begin() as connection:
            connection.execute(sa.update(platform.physical_root).values(mode="adopt_ro"))
        series = env.new_series()
        env.add_chapter(series, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert not (env.lib.library / RESERVED_DIR / "manifest").exists()
        assert mirror_of(env).rebuild(env.destination_id) == []


def test_the_second_run_rewrites_nothing() -> None:
    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        (path,) = mirror_files(env)
        stamp = path.stat().st_ino, path.stat().st_mtime_ns
        with env.engine.connect() as connection:
            intents = connection.execute(sa.text("SELECT count(*) FROM fs_intent")).scalar_one()
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert (path.stat().st_ino, path.stat().st_mtime_ns) == stamp
        with env.engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM fs_intent")).scalar_one() == (
                intents
            )


def test_a_run_heals_a_lost_mirror() -> None:
    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        shutil.rmtree(env.lib.library / RESERVED_DIR / "manifest")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert len(mirror_files(env)) == 1


def test_a_pull_destination_has_no_mirror() -> None:
    with export_env(transport="pull") as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert not (env.lib.library / RESERVED_DIR / "manifest").exists()
