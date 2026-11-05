"""Planner, dry-run/apply, manifest and export series over real files and a real database."""

import io
import zipfile

import pytest
import sqlalchemy as sa

import shelfhold.export.application.pipeline as pipeline_module
from shelfhold.export.application.export_items import CoverageError, add_coverage
from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.domain.export_item import check_coverage
from shelfhold.infrastructure.schema import export
from tests.export_env import add_second_shelf, export_env, sha, state_of


@pytest.mark.req("EF-EXP-008")
def test_dry_run_and_apply_share_planner(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = pipeline_module.plan_destination

    def spy(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pipeline_module, "plan_destination", spy)
    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "2")
        env.add_chapter(series, "3")
        preview = env.pipeline.preview(env.destination_id)
        assert env.files() == []  # a dry run writes nothing
        assert env.rows(export.export_record) == []
        applied = env.pipeline.run(env.destination_id, job_id="job-1")
        assert len(calls) == 2  # the one planner, once for each
        assert applied.ops == preview.ops  # the operations are identical
        assert sorted(p.name for p in env.files()) == [
            "Dr Stone - Ch.2.cbz",
            "Dr Stone - Ch.3.cbz",
        ]


@pytest.mark.req("EF-EXP-008")
def test_apply_after_a_change_is_refused_and_the_preview_recomputed() -> None:
    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "2")
        preview = env.pipeline.preview(env.destination_id)
        env.add_chapter(series, "3")  # a chapter arrived between the preview and the click
        with pytest.raises(StalePlanError) as raised:
            env.pipeline.apply(preview.plan_id, if_match=preview.base_version, job_id="job-1")
        assert raised.value.status == 412
        assert env.files() == []
        fresh = env.pipeline.preview(env.destination_id)
        assert fresh.base_version != preview.base_version and len(fresh.ops) == 2
        env.pipeline.apply(fresh.plan_id, if_match=fresh.base_version, job_id="job-2")
        assert len(env.files()) == 2


@pytest.mark.req("EF-EXP-007")
def test_export_idempotent_second_run_noop() -> None:
    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "2")
        env.add_chapter(series, "3")
        first = env.pipeline.run(env.destination_id, job_id="job-1")
        assert len(first.ops) == 2
        before = state_of(env)
        env.scans.clear()
        second = env.pipeline.run(env.destination_id, job_id="job-2")
        assert second.ops == ()
        assert state_of(env) == before  # no write, no intent, no manifest change
        assert env.scans == []  # and nothing to scan


@pytest.mark.req("EF-EXP-007")
def test_rendered_file_differs_from_the_original_but_not_its_content_hash() -> None:
    with export_env() as env:
        series = env.new_series()
        unit = env.add_chapter(series, "2")
        env.pipeline.run(env.destination_id, job_id="job-1")
        (record,) = env.rows(export.export_record)
        (path,) = env.files()
        data = path.read_bytes()
        assert record.file_hash == sha(data)  # the manifest keeps the hash of the rendered file
        with env.engine.connect() as connection:
            content = connection.execute(
                sa.text("SELECT content_hash, original_sha256 FROM content WHERE unit_id = :u"),
                {"u": unit},
            ).one()
        assert record.content_hash == content.content_hash  # the payload is the one received
        assert record.file_hash != content.original_sha256  # ComicInfo was added by the render
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        assert names[-1] == "ComicInfo.xml" and names[:-1] == ["2-001.png"]


@pytest.mark.req("EF-EXP-058")
def test_manifest_keyed_by_shelf_destination_and_export_item() -> None:
    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "2")
        env.pipeline.run(env.destination_id, job_id="job-1")
        (record,) = env.rows(export.export_record)
        (item,) = env.rows(export.export_item)
        assert record.shelf_destination_id == env.destination_id
        assert record.export_item_id == item.id
        assert (record.locator_type, record.rel_path) == ("path", "Dr Stone/Dr Stone - Ch.2.cbz")
        assert record.state == "written" and record.template_ref == "kavita-manga@1"
        assert item.frozen_attrs["series"] == "Dr Stone"
        pk = [c.name for c in export.export_record.primary_key.columns]
        assert pk == ["shelf_destination_id", "export_item_id"]
        assert env.scans == ["/manga/Kavita/Dr Stone"]  # one targeted scan, by remote path


@pytest.mark.req("EF-BIB-044", "EF-EXP-058")
def test_shelfhold_written_export_item_covers_exactly_one_unit() -> None:
    with export_env() as env:
        series = env.new_series()
        first = env.add_chapter(series, "2")
        second = env.add_chapter(series, "3")
        env.pipeline.run(env.destination_id, job_id="job-1")
        coverage = env.rows(export.unit_coverage)
        assert len(coverage) == len(env.rows(export.export_item)) == 2
        assert {c.unit_id for c in coverage} == {first, second}
        item = env.rows(export.export_item)[0]
        with env.engine.begin() as connection, pytest.raises(CoverageError):
            add_coverage(connection, item.id, second if coverage[0].unit_id == first else first)
    with pytest.raises(CoverageError):
        check_coverage("unit", ["u-1", "u-2"])
    with pytest.raises(CoverageError):
        check_coverage("unit", [])
    check_coverage("adopted", ["u-1", "u-2", "u-3"])  # an adopted volume covers several


@pytest.mark.req("EF-EXP-062")
def test_export_series_id_never_recomputed() -> None:
    with export_env() as env:
        series = env.new_series("Dr Stone")
        env.add_chapter(series, "2")
        env.pipeline.run(env.destination_id, job_id="job-1")
        (first,) = env.rows(export.export_series)
        with env.engine.begin() as connection:  # the Work is renamed afterwards
            connection.execute(sa.text("UPDATE work SET title = 'Dr. Stone (new)'"))
        env.add_chapter(series, "3")
        plan = env.pipeline.run(env.destination_id, job_id="job-2")
        (after,) = env.rows(export.export_series)
        assert after.id == first.id and after.name_raw == "Dr Stone"
        assert [op.kind for op in plan.ops] == ["write"]  # a new chapter, no rename of the old
        assert sorted(p.parent.name for p in env.files()) == ["Dr Stone", "Dr Stone"]
        assert len(env.rows(export.edition_export_series)) == 1


@pytest.mark.req("EF-EXP-062")
def test_second_shelf_on_the_same_remote_library_is_blocked_by_the_loaded_series() -> None:
    with export_env() as env:
        series = env.new_series("Monster")
        env.add_chapter(series, "2")
        env.pipeline.run(env.destination_id, job_id="job-1")
        other_shelf, other_destination = add_second_shelf(env)
        other = env.new_series("monster", shelf_id=other_shelf)
        env.add_chapter(other, "2")
        preview = env.pipeline.preview(other_destination)
        assert [b.code for b in preview.blocked] == ["export.series_name_collision"]
        assert preview.ops == ()
        assert {w.code for w in preview.warnings} == {"age_tier_mix"}  # 18 and X in one library

