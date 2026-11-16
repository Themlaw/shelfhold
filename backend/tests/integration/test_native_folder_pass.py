"""EF-EXP-010, EF-EXP-001, EF-EXP-002, EF-BIB-045 (M4 L07, intouchable : intégrité): a pass over a
native folder writes the local layout of Mihon with the bytes the host owns.

Real database, real files, the executor of L04 and the fan-out of L06: the destination only
describes (`NativeFolderSource` reads the planned items and returns the plan), the host serves the
bytes, the chapter with a ComicInfo made for it (Translator = the team) and the cover of the
series, and writes them. The attacks: another shelf's item (IDOR), the family barrier, a ceiling
lowered after the binding, and a plan that cites what nobody planned.
"""

import io
import zipfile

import pytest
import sqlalchemy as sa

from shelfhold.export.application.fan_out import FanOut, HostContentStore, planned_content
from shelfhold.export.application.plan_executor import PlanRefusedError
from shelfhold.export.domain.bindings import UnpartitionedDestinationError
from shelfhold.export.targets.native_folder.source import NativeFolderSource
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.sdk.fsops import ContentRef
from tests.byte_source_env import Unit, unit_with_item
from tests.export_env import ExportEnv, Series, add_second_shelf, export_env
from tests.fan_out_env import add_target, bind, records_of
from tests.plan_executor_env import Probe

TEAM = "Synthetic Team A"


def native(env: ExportEnv, subpath: str = "local", *, link: bool = False) -> str:
    return bind(
        env,
        subpath,
        target_id=add_target(env, kind="native_folder", partitioning="none"),
        link=link,
    )


def run(env: ExportEnv, call_id: str = "c1") -> tuple[FanOut, NativeFolderSource]:
    source = NativeFolderSource(env.engine)
    host = HostContentStore(env.engine, capabilities=source.capabilities, renders=source.renders)
    env.pipeline.use_plan_executor(contents=host, probe=Probe(None))
    return FanOut(env.engine, env.pipeline, source, clock=env.clock), source


def tree(env: ExportEnv, subpath: str = "local") -> list[str]:
    root = env.lib.manga / subpath
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def titled(env: ExportEnv, series: Series, unit: Unit, title: str, team: str = TEAM) -> None:
    team_id = new_ulid()
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.update(catalog.unit).where(catalog.unit.c.id == unit.unit_id).values(title=title)
        )
        connection.execute(
            sa.insert(catalog.team).values(id=team_id, edition_id=series.edition_id, label=team)
        )
        connection.execute(
            sa.update(catalog.unit_variant)
            .where(catalog.unit_variant.c.unit_id == unit.unit_id)
            .values(team_id=team_id, preferred=True)
        )


@pytest.mark.req("EF-EXP-010", "EF-EXP-001")
def test_a_pass_writes_the_mihon_local_tree_with_a_comicinfo_and_a_cover() -> None:
    with export_env() as env:
        series = env.new_series("Dr Stone")
        one, two = unit_with_item(env, series, "1"), unit_with_item(env, series, "2.5")
        titled(env, series, one, "Dawn")
        destination = native(env)
        fan, _source = run(env)

        report = fan.run(env.shelf_id, "c1")

        outcome = {o.destination_id: o for o in report.outcomes}
        assert outcome[destination].status == "done", outcome[destination]
        assert tree(env) == [
            "Dr Stone/Ch.0001 Dawn.cbz",
            "Dr Stone/Ch.0002.5.cbz",
            "Dr Stone/cover.jpg",
        ]
        assert not [p for p in tree(env) if p.casefold().endswith(".json")]
        chapter = env.lib.manga / "local" / "Dr Stone" / "Ch.0001 Dawn.cbz"
        with zipfile.ZipFile(io.BytesIO(chapter.read_bytes())) as archive:
            names = archive.namelist()
            info = archive.read("ComicInfo.xml").decode()
        assert names[-1] == "ComicInfo.xml" and "1-001.png" in names
        assert f"<Translator>{TEAM}</Translator>" in info and "<Title>Dawn</Title>" in info
        cover = (env.lib.manga / "local" / "Dr Stone" / "cover.jpg").read_bytes()
        assert cover.startswith(
            b"\x89PNG"
        )  # the first page as it is: Mihon reads cover.* by content
        assert {r.export_item_id for r in records_of(env, destination)} == {
            one.item_id,
            two.item_id,
        }
        for path in (env.lib.manga / "local").rglob("*.cbz"):  # a file of its own: no shared link
            assert path.stat().st_nlink == 1


@pytest.mark.req("EF-EXP-010")
def test_a_second_pass_changes_nothing_and_a_new_chapter_adds_one_file() -> None:
    with export_env() as env:
        series = env.new_series("Dr Stone")
        unit_with_item(env, series, "1")
        native(env)
        fan, _source = run(env)
        fan.run(env.shelf_id, "c1")
        first = {p: (env.lib.manga / "local" / p).stat().st_mtime_ns for p in tree(env)}

        again = fan.run(env.shelf_id, "c2")
        assert {o.status for o in again.outcomes} <= {"done", "skipped"}
        assert {p: (env.lib.manga / "local" / p).stat().st_mtime_ns for p in tree(env)} == first

        unit_with_item(env, series, "2")
        fan.run(env.shelf_id, "c3")
        assert len(tree(env)) == len(first) + 1


@pytest.mark.req("EF-EXP-010", "EF-EXP-002")
def test_the_native_folder_registers_a_local_remote_library() -> None:
    """M1a: an exported series needs a remote library; a folder destination has a local one."""
    with export_env() as env:
        destination = native(env)
        with env.engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    export.remote_library.c.remote_ref, export.remote_library.c.export_target_id
                )
                .select_from(
                    export.remote_library.join(
                        export.shelf_destination_remote_library,
                        export.shelf_destination_remote_library.c.remote_library_id
                        == export.remote_library.c.id,
                    )
                )
                .where(
                    export.shelf_destination_remote_library.c.shelf_destination_id == destination
                )
            ).all()
        assert len(rows) == 1 and rows[0].remote_ref == "local"


# -- the attacks ---------------------------------------------------------------------------------


@pytest.mark.req("EF-EXP-010", "EF-EXP-047")
def test_an_item_of_another_shelf_never_reaches_the_native_folder() -> None:
    """IDOR: the bytes of an Edition filed on another shelf are not the binding's to cite."""
    with export_env() as env:
        mine = unit_with_item(env, env.new_series("Mine"), "1")
        other_shelf, _destination = add_second_shelf(env)
        theirs = unit_with_item(env, env.new_series("Theirs", shelf_id=other_shelf), "1")
        destination = native(env)
        fan, source = run(env)

        fan.run(env.shelf_id, "c1")

        assert tree(env) == ["Mine/Ch.0001.cbz", "Mine/cover.jpg"]
        assert not any("Theirs" in p for p in tree(env))
        host = HostContentStore(
            env.engine, capabilities=source.capabilities, renders=source.renders
        )
        for ref in (
            ContentRef(kind="export_item", ref=theirs.item_id),
            ContentRef(kind="sidecar", ref=f"cover:{theirs.item_id}"),
        ):
            with pytest.raises(PlanRefusedError):
                host.read(destination, ref)
        assert mine.item_id in planned_content(env.engine.connect(), destination).planned_refs
        assert theirs.item_id not in planned_content(env.engine.connect(), destination).planned_refs


@pytest.mark.req("EF-EXP-010", "EF-EXP-047")
def test_a_shelf_below_18_cannot_be_bound_to_a_folder_that_keeps_no_accounts_apart() -> None:
    with export_env(max_age_rating=13) as env, pytest.raises(UnpartitionedDestinationError):
        native(env)


@pytest.mark.req("EF-EXP-010", "EF-EXP-047")
def test_a_ceiling_lowered_after_the_binding_stops_the_writes() -> None:
    with export_env(max_age_rating=99) as env:
        unit_with_item(env, env.new_series("Dr Stone"), "1")
        destination = native(env)
        fan, _source = run(env)
        fan.run(env.shelf_id, "c1")
        assert tree(env)
        written = tree(env)
        unit_with_item(env, env.new_series("Late"), "1")

        with write_transaction(env.engine) as connection:  # the shelf is now below 18+
            connection.execute(
                sa.update(export.shelf)
                .where(export.shelf.c.id == env.shelf_id)
                .values(max_age_rating=13)
            )
        report = fan.run(env.shelf_id, "c2")

        outcome = {o.destination_id: o for o in report.outcomes}[destination]
        assert outcome.status == "refused" and outcome.code == "shelf.destination_not_partitioned"
        assert tree(env) == written  # nothing new, nothing removed


class Describes:
    """A destination that says what it likes, over the real source (capabilities, sidecars)."""

    def __init__(self, inner: NativeFolderSource, plan: object) -> None:
        self._inner, self._plan = inner, plan

    def capabilities(self, target_id: str) -> object:
        return self._inner.capabilities(target_id)

    def sidecars(self, binding: object) -> frozenset[str]:
        return self._inner.sidecars(binding)  # type: ignore[arg-type]

    def plan(self, _binding: object) -> object:
        return self._plan

    def calls(self, _binding: object) -> tuple[object, ...]:
        return ()


@pytest.mark.req("EF-EXP-010")
@pytest.mark.parametrize("name", ["details.json", "Details.JSON", "series.json", "metadata.json"])
def test_a_plan_that_writes_a_json_file_is_refused_whatever_the_destination_says(name: str) -> None:
    with export_env() as env:
        unit = unit_with_item(env, env.new_series("Dr Stone"), "1")
        destination = native(env)
        _fan, source = run(env)
        plan = {
            "ops": [
                {"op": "mkdir", "rel_path": "Dr Stone"},
                {
                    "op": "write",
                    "rel_path": f"Dr Stone/{name}",
                    "content_ref": {"kind": "export_item", "ref": unit.item_id},
                },
            ]
        }
        fan = FanOut(env.engine, env.pipeline, Describes(source, plan), clock=env.clock)  # type: ignore[arg-type]
        report = fan.run(env.shelf_id, "c9")
        outcome = {o.destination_id: o for o in report.outcomes}[destination]
        assert outcome.status == "failed" and outcome.code == "export.plan_refused"
        assert tree(env) == []
