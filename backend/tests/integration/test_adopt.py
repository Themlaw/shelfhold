"""L29: adopting an existing adult Kavita library (EF-EXP-028, EF-EXP-045, EF-EXP-052, EF-EXP-055).

Untouchable: adoption never rewrites, moves or deletes an existing file. It reads, it records.
Renames stay a preview; nothing is entered before the admin confirms; a library that children can
read is refused before M1b.
"""

import pytest
import sqlalchemy as sa

from shelfhold.export.application.target_state import (
    CHILD_EXPOSURE,
    DestinationPausedError,
    pause_shelf,
)
from shelfhold.export.targets.kavita import ConfirmationRequired
from shelfhold.export.targets.kavita.adopt import AdoptionRefused
from shelfhold.infrastructure.schema import catalog, export, storage
from tests.adopt_env import Adoption, adoption, comicinfo
from tests.export_env import ExportEnv, export_env
from tests.fakes.kavita import member
from tests.kavita_env import run
from tests.kavita_export_env import LIBRARY_ID

PATTERN = "**/.shelfhold/**"


def _count(env: ExportEnv, table: sa.Table) -> int:
    with env.engine.connect() as connection:
        return int(connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())


def _library(a: Adoption) -> None:
    """Two series: a chapter series and a series of volumes only."""
    a.add_series(
        "Dr Stone",
        ["Dr Stone - Ch.1.cbz", "Dr Stone - Ch.2.cbz", "Dr Stone - Ch.3 [Team].cbz"],
        info=comicinfo("Dr Stone", age="Mature 17+"),
    )
    a.add_series("Monster", ["Monster - Vol.1.cbz", "Monster - Vol.2.cbz"])


def _adopt(a: Adoption, *, confirmed: bool = True) -> object:
    return run(a.adopter.adopt(a.wired.client, a.env.destination_id, confirmed=confirmed))


@pytest.mark.req("EF-EXP-028")
def test_adopt_existing_kavita_library_without_rewrite() -> None:
    with export_env() as env:
        a = adoption(env)
        _library(a)
        before = a.state()
        _adopt(a)
        assert a.state() == before  # not a byte, a name, an mtime or an inode moved
        assert _count(env, export.export_record) == 5
        with env.engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM fs_intent").scalar_one() == 0
        # Kavita: only the library settings may change (the exclusion of our reserved folder)
        writes = [(r.method, r.path.lower()) for r in a.wired.fake.writes]
        assert writes == [("POST", "/api/library/update")]
        assert a.wired.fake.scans == []  # no scan: nothing was written to be found
        patterns = a.wired.fake.libraries[0]["excludePatterns"]
        assert patterns[:1] == ["*.tmp"] and PATTERN in patterns  # the admin's own are kept


@pytest.mark.req("EF-EXP-028", "EF-BIB-044")
def test_adopted_units_registered_in_manifest_as_adopted() -> None:
    with export_env() as env:
        a = adoption(env)
        _library(a)
        _adopt(a)
        with env.engine.connect() as connection:
            records = connection.execute(sa.select(export.export_record)).mappings().all()
            kinds = {
                r.id: r.kind for r in connection.execute(sa.select(export.export_item)).mappings()
            }
            coverage = connection.execute(sa.select(export.unit_coverage)).mappings().all()
            states = (
                connection.execute(
                    sa.select(catalog.unit.c.state)
                    .select_from(
                        catalog.unit.join(
                            export.unit_coverage,
                            export.unit_coverage.c.unit_id == catalog.unit.c.id,
                        )
                    )
                    .distinct()
                )
                .scalars()
                .all()
            )
        assert {r["state"] for r in records} == {"adopted"}
        assert {r["locator_type"] for r in records} == {"path"}
        assert {r["rel_path"] for r in records} == {
            "Dr Stone/Dr Stone - Ch.1.cbz",
            "Dr Stone/Dr Stone - Ch.2.cbz",
            "Dr Stone/Dr Stone - Ch.3 [Team].cbz",
            "Monster/Monster - Vol.1.cbz",
            "Monster/Monster - Vol.2.cbz",
        }
        assert all(r["file_hash"] and r["size"] and r["mtime_ns"] for r in records)
        assert set(kinds.values()) == {"adopted"}
        assert {c["carrier_type"] for c in coverage} == {"export_item"}
        assert len(coverage) >= 5  # every adopted file carries at least one Unit
        assert all(c["extent"] == "full" and c["origin"] == "filename" for c in coverage)
        assert set(states) == {"adopted"}
        chapter_three = connection_unit_number(env, "Dr Stone/Dr Stone - Ch.3 [Team].cbz")
        assert chapter_three == "3"  # read through plugin/parse, not guessed from the name


def connection_unit_number(env: ExportEnv, rel_path: str) -> str:
    with env.engine.connect() as connection:
        return str(
            connection.execute(
                sa.select(catalog.unit.c.number_norm)
                .select_from(
                    export.export_record.join(
                        export.unit_coverage,
                        export.unit_coverage.c.carrier_id == export.export_record.c.export_item_id,
                    ).join(catalog.unit, catalog.unit.c.id == export.unit_coverage.c.unit_id)
                )
                .where(export.export_record.c.rel_path == rel_path)
            ).scalar_one()
        )


@pytest.mark.req("EF-EXP-028")
def test_adopted_files_referenced_not_copied() -> None:
    with export_env() as env:
        a = adoption(env)
        _library(a)
        before = set(a.state())
        _adopt(a)
        assert set(a.state()) == before  # no file anywhere: not in the store, not in the library
        with env.engine.connect() as connection:
            contents = connection.execute(sa.select(storage.content)).mappings().all()
            replicas = connection.execute(sa.text("SELECT * FROM replica")).mappings().all()
        assert contents and {c["retention"] for c in contents} == {"adopted_ref"}
        assert all(c["path"] is None for c in contents)  # nothing kept: referenced in place
        assert len(replicas) == 5
        assert all(r["location"] == "destination" and r["rel_path"] for r in replicas)
        assert all(r["independent"] == 0 and r["mutable_by_third_party"] == 1 for r in replicas)


@pytest.mark.req("EF-EXP-028")
def test_adoption_proposes_renames_only_as_preview() -> None:
    with export_env() as env:
        a = adoption(env)
        _library(a)
        before = a.state()
        report = run(a.adopter.preview(a.wired.client, env.destination_id))
        proposals = {(p.from_path, p.to_path) for p in report.renames}
        assert ("Dr Stone/Dr Stone - Ch.3 [Team].cbz", "Dr Stone/Dr Stone - Ch.3.cbz") in proposals
        assert not any(p.from_path == "Dr Stone/Dr Stone - Ch.1.cbz" for p in report.renames)
        assert a.state() == before and _count(env, export.export_record) == 0  # a preview
        _adopt(a)
        assert a.state() == before  # confirmed, adopted: the odd name is still the odd name
        with env.engine.connect() as connection:
            paths = set(connection.execute(sa.select(export.export_record.c.rel_path)).scalars())
        assert (
            "Dr Stone/Dr Stone - Ch.3 [Team].cbz" in paths
        )  # the manifest says where it really is
        env.pipeline.run(env.destination_id, job_id="01J00000000000000000000000")
        assert a.state() == before  # and a later export does not apply the proposal either


@pytest.mark.req("EF-EXP-028", "EF-EXP-009")
def test_adopted_library_never_modified_without_confirmation() -> None:
    with export_env() as env:
        a = adoption(env)
        _library(a)
        before = a.state()
        with pytest.raises(ConfirmationRequired):
            _adopt(a, confirmed=False)
        assert a.wired.fake.writes == []  # not a single write route called
        assert _count(env, export.export_record) == 0 and _count(env, export.export_item) == 0
        with env.engine.connect() as connection:
            row = connection.execute(sa.select(export.shelf_destination)).mappings().one()
        assert not row["adopted"] and row["pre_adoption_settings"] is None
        assert a.state() == before
        run(a.adopter.preview(a.wired.client, env.destination_id))  # the preview is free
        assert a.wired.fake.writes == [] and _count(env, export.export_record) == 0
        _adopt(a, confirmed=True)
        assert _count(env, export.export_record) == 5


@pytest.mark.req("EF-EXP-028", "EF-EXP-011")
def test_adoption_reverse_age_mapping_stored_as_suggestion() -> None:
    with export_env() as env:
        a = adoption(env)
        _library(a)  # Dr Stone carries `Mature 17+`
        _adopt(a)
        with env.engine.connect() as connection:
            suggestions = connection.execute(sa.select(catalog.age_suggestion)).mappings().all()
            ratings = connection.execute(
                sa.select(catalog.work.c.age_rating, catalog.edition.c.age_rating).select_from(
                    catalog.work.join(catalog.edition)
                )
            ).all()
        assert [
            (s["entity_type"], s["value"], s["x"], s["provenance"], s["state"]) for s in suggestions
        ] == [("edition", 16, False, "comicinfo", "open")]
        assert all(w is None and e is None for w, e in ratings)  # suggested, never decided


@pytest.mark.req("EF-EXP-028")
def test_pre_adoption_settings_recorded() -> None:
    with export_env() as env:
        a = adoption(env)
        _library(a)
        _adopt(a)
        with env.engine.connect() as connection:
            row = connection.execute(sa.select(export.shelf_destination)).mappings().one()
        assert row["adopted"] is True or row["adopted"] == 1
        assert row["pre_adoption_settings"] == {
            "library_type": "Manga",
            "exclude_patterns": ["*.tmp"],  # as Kavita had them, before ours was added
        }


@pytest.mark.req("EF-EXP-045")
def test_adopted_same_number_kept_and_linked_never_replaced() -> None:
    with export_env() as env:
        a = adoption(env)
        series = env.new_series("Dr Stone")
        unit = env.add_chapter(series, "112")  # received from the source
        a.add_series("Dr Stone", ["Dr Stone - Ch.112.cbz"], info=comicinfo("Dr Stone"))
        before = a.state()
        adopted = {p: v for p, v in before.items() if p.startswith("manga/Kavita/Dr Stone/")}
        _adopt(a)
        with env.engine.connect() as connection:
            record = connection.execute(sa.select(export.export_record)).mappings().one()
            covered = (
                connection.execute(
                    sa.select(export.unit_coverage.c.unit_id).where(
                        export.unit_coverage.c.carrier_id == record["export_item_id"]
                    )
                )
                .scalars()
                .all()
            )
            units = connection.execute(sa.select(catalog.unit.c.id)).scalars().all()
        assert covered == [unit]  # the adopted file is the exported Variant of that very Unit
        assert record["state"] == "adopted" and len(units) == 2  # (the fixture chain has one)
        plan = env.pipeline.preview(env.destination_id)
        assert len(plan.ops) == 0 and len(plan.blocked) == 0  # nothing is written for Ch.112
        assert not any(w.code == "content_changed" for w in plan.warnings)
        env.pipeline.run(env.destination_id, job_id="01J00000000000000000000000")
        assert {p: v for p, v in a.state().items() if p in adopted} == adopted
        assert len(list(env.root.glob("Dr Stone/*Ch.112*"))) == 1  # one Ch.112, the adopted one


@pytest.mark.req("EF-EXP-045")
def test_adoption_report_lists_existing_same_number_duplicates() -> None:
    with export_env() as env:
        a = adoption(env)
        a.add_series(
            "Dr Stone", ["Dr Stone - Ch.5 (HD).cbz", "Dr Stone - Ch.5.cbz", "Dr Stone - Ch.6.cbz"]
        )
        before = a.state()
        report = run(a.adopter.preview(a.wired.client, env.destination_id))
        assert [(d.series, d.number, d.files) for d in report.duplicates] == [
            (
                "Dr Stone",
                "5",
                ("Dr Stone - Ch.5 (HD).cbz", "Dr Stone - Ch.5.cbz"),
            )
        ]
        _adopt(a)
        assert a.state() == before  # reported and proposed, never deleted
        assert _count(env, export.export_record) == 3


@pytest.mark.req("EF-EXP-028", "EF-EXP-055")
def test_child_library_adoption_refused_before_m1b() -> None:
    with export_env() as env:
        a = adoption(env)
        _library(a)
        a.wired.fake.users.append(member("enfant", libraries=(LIBRARY_ID,), age_rating=6))
        with pytest.raises(AdoptionRefused) as refused:
            run(a.adopter.preview(a.wired.client, env.destination_id))
        assert refused.value.accounts == ("enfant",)  # the list of accounts is given
        with pytest.raises(AdoptionRefused):
            _adopt(a)
        assert a.wired.fake.writes == []  # no write in Kavita
        assert _count(env, export.export_record) == 0  # nothing entered


@pytest.mark.req("EF-EXP-055")
def test_shelf_link_refused_when_library_exposed_to_children() -> None:
    with export_env() as env:
        a = adoption(env)
        with env.engine.begin() as connection:  # not linked yet
            connection.execute(sa.delete(export.shelf_destination_remote_library))
            connection.execute(sa.delete(export.remote_library))
        a.wired.fake.users.append(member("ado", libraries=(LIBRARY_ID,), include_unknowns=False))
        with pytest.raises(AdoptionRefused) as refused:
            run(a.adopter.link_library(a.wired.client, env.destination_id, str(LIBRARY_ID)))
        assert refused.value.accounts == ("ado",)
        assert _count(env, export.shelf_destination_remote_library) == 0
        assert a.wired.fake.writes == []
        a.wired.fake.users.pop()  # the account is gone: the audit is replayed at the link
        run(a.adopter.link_library(a.wired.client, env.destination_id, str(LIBRARY_ID)))
        assert _count(env, export.shelf_destination_remote_library) == 1
        assert _count(env, export.kavita_access_audit) == 1  # AccessAuditor.audit ran at the link


@pytest.mark.req("EF-EXP-055")
def test_kavita_client_is_built_with_the_write_guard() -> None:
    from shelfhold.export.targets.kavita.wiring import guarded_client

    with export_env() as env:
        a = adoption(env)
        client = guarded_client(a.wired.client, env.engine, env.target_id)
        run(client.ensure_exclude_patterns(LIBRARY_ID, [PATTERN]))  # not paused: it writes
        with env.engine.begin() as connection:
            pause_shelf(connection, env.shelf_id, CHILD_EXPOSURE)
        before = len(a.wired.fake.writes)
        with pytest.raises(DestinationPausedError):
            run(client.ensure_exclude_patterns(LIBRARY_ID, ["*.bak"]))
        assert len(a.wired.fake.writes) == before  # the guard answers before any request


@pytest.mark.req("EF-EXP-028", "EF-BIB-044")
def test_adopted_volume_only_series_never_auto_follows() -> None:
    with export_env() as env:
        a = adoption(env)
        _library(a)
        report = run(a.adopter.preview(a.wired.client, env.destination_id))
        assert {s.name: s.volume_only for s in report.series} == {
            "Dr Stone": False,
            "Monster": True,
        }
        _adopt(a)
        assert _count(env, export.export_record) == 5
        with env.engine.connect() as connection:
            follows = connection.exec_driver_sql("SELECT count(*) FROM series_follow").scalar_one()
            monster = connection.execute(
                sa.select(catalog.unit.c.track, catalog.unit.c.state)
                .select_from(
                    catalog.unit.join(
                        export.unit_coverage, export.unit_coverage.c.unit_id == catalog.unit.c.id
                    ).join(
                        export.export_record,
                        export.export_record.c.export_item_id == export.unit_coverage.c.carrier_id,
                    )
                )
                .where(export.export_record.c.rel_path.like("Monster/%"))
            ).all()
        assert follows == 0  # adoption never starts a follow, whatever the series
        assert [tuple(r) for r in monster] == [("unnumbered", "adopted")] * 2  # range unknown


@pytest.mark.req("EF-EXP-028", "EF-INT-014")
def test_parse_bulk_used_for_replay_and_adoption() -> None:
    from shelfhold.export.targets.kavita.adopt import BulkParse

    with export_env() as env:
        a = adoption(env)
        _library(a)
        _adopt(a)
        calls = a.wired.fake.parse_calls
        assert {c["name"] for c in calls} >= {"Dr Stone - Ch.3 [Team].cbz", "Monster - Vol.2.cbz"}
        assert a.wired.fake.requests_to("/api/plugin/parse-bulk")  # in batches ...
        assert not [r for r in a.wired.fake.requests_to("/api/plugin/parse") if r.query]  # ... only
        # replay: the names a plan asks for are fetched in bulk, then the plan may be applied
        series = env.new_series("Blue Lock")
        env.add_chapter(series, "9")
        parse = run(BulkParse.for_plan(a.wired.client, env.pipeline, env.destination_id))
        env.pipeline.bind_parse(parse, verified=True)
        report = env.pipeline.run(env.destination_id, job_id="01J00000000000000000000000")
        assert report is not None and len(env.files()) == 6
        assert a.wired.fake.requests_to("/api/plugin/parse-bulk")
