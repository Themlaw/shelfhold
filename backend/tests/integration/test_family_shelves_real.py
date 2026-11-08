"""L10 (EF-EXP-046, EF-EXP-047, EF-EXP-050, EF-EXP-055, ENF-COMP-001) over a real instance: the
shelf command, the Kavita linker and the access audit against the simulated Kavita, the follow
cycle and the export job as the scheduler runs them."""

import asyncio
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.domain.bindings import LibraryNotDedicatedError
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.linking import ShelfLinker
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export, storage
from shelfhold.library.public import keep_candidates
from shelfhold.ops.events import BusTargetEvents
from tests.fakes.kavita.server import default_libraries, member
from tests.l50_env import Seeded, seeded
from tests.l54_env import bench54
from tests.library_env import cbz_bytes


def family(env: Seeded, name: str = "Famille") -> str:
    """`shelfhold shelf create Famille --max-age 13+`: the destination of the new shelf."""
    env.inst.admin.shelf_create(name=name, max_age="13+", target="k", subpath=name)
    with env.inst.engine.connect() as connection:
        return str(
            connection.execute(
                sa.select(export.shelf_destination.c.id)
                .join(export.shelf, export.shelf.c.id == export.shelf_destination.c.shelf_id)
                .where(export.shelf.c.name == name)
            ).scalar_one()
        )


def link(env: Seeded, destination_id: str, name: str = "Famille") -> str:
    inst = env.inst
    linker = ShelfLinker(
        inst.engine, inst.comp.clock,
        AccessAuditor(inst.engine, inst.comp.clock, BusTargetEvents(inst.engine, inst.comp.bus)),
    )  # fmt: skip
    target = env.inst.comp.kavita_client(_target_of(env, destination_id))
    return asyncio.run(linker.link(target, destination_id, library_name=name))


def _target_of(env: Seeded, destination_id: str) -> str:
    with env.inst.engine.connect() as connection:
        return str(
            connection.execute(
                sa.select(export.shelf_destination.c.target_id).where(
                    export.shelf_destination.c.id == destination_id
                )
            ).scalar_one()
        )


def dedicated_adult_library(env: Seeded) -> None:
    """Kavita as a careful admin set it up: the adult library watches its own folder only."""
    lib = default_libraries()[0]
    env.inst.kavita.libraries[:] = [{**lib, "folders": ["/manga/Kavita"]}]


@pytest.mark.req("EF-EXP-047", "EF-EXP-055", "EF-USR-015")
def test_family_shelf_targets_dedicated_kavita_library(settings: Settings, tmp_path: Path) -> None:
    with seeded(settings, tmp_path) as env:
        dedicated_adult_library(env)
        destination = family(env)
        ref = link(env, destination)
        created = [lib for lib in env.inst.kavita.libraries if str(lib["id"]) == ref]
        assert created and created[0]["folders"] == ["/manga/Famille"]  # its own folder, no more
        with env.inst.engine.connect() as connection:
            library = connection.execute(
                sa.select(export.shelf_destination_remote_library.c.remote_library_id).where(
                    export.shelf_destination_remote_library.c.shelf_destination_id == destination
                )
            ).scalar_one()
            adult = connection.execute(
                sa.select(export.shelf_destination_remote_library.c.remote_library_id).where(
                    export.shelf_destination_remote_library.c.shelf_destination_id
                    == env.destination_id
                )
            ).scalar_one()
        assert library != adult  # two shelves never share a remote library


@pytest.mark.req("EF-EXP-047", "EF-EXP-055")
def test_family_shelf_over_a_shared_kavita_library_is_refused(
    settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path) as env:
        destination = family(env)  # library 7 watches /manga: the adult shelf is in it too
        before = len(env.inst.kavita.libraries)
        with pytest.raises(LibraryNotDedicatedError) as refused:
            link(env, destination)
        assert refused.value.code == "shelf.library_not_dedicated"
        assert len(env.inst.kavita.libraries) == before and not env.inst.kavita.writes
        with env.inst.engine.connect() as connection:
            linked = connection.execute(
                sa.select(sa.func.count())
                .select_from(export.shelf_destination_remote_library)
                .where(
                    export.shelf_destination_remote_library.c.shelf_destination_id == destination
                )
            ).scalar_one()
        assert linked == 0


@pytest.mark.req("ENF-COMP-001", "EF-EXP-055")
def test_kavita_known_bad_access_control_blocks_family_shelf(
    settings: Settings, tmp_path: Path
) -> None:
    """Kavita <= 0.9.0.2: no account is restricted and still the family shelf is blocked."""
    with seeded(settings, tmp_path) as env:
        dedicated_adult_library(env)
        destination = family(env)
        link(env, destination)
        inst = env.inst
        inst.kavita.version = "0.9.0.2"
        client = inst.comp.kavita_client(_target_of(env, destination))
        auditor = AccessAuditor(
            inst.engine, inst.comp.clock, BusTargetEvents(inst.engine, inst.comp.bus)
        )
        report = asyncio.run(auditor.audit(client, _target_of(env, destination)))
        with inst.engine.connect() as connection:
            paused = dict(
                connection.execute(
                    sa.select(export.shelf.c.name, export.shelf.c.paused_reason)
                ).all()
            )
        assert paused["Famille"] == "child_exposure"  # blocked, not warned
        assert paused["Adultes"] is None  # no restricted account: the adult shelf only warns
        assert report.paused_shelves
        inst.kavita.version = "0.9.1.4"
        inst.kavita.users.append(member("kid", libraries=(), age_rating=5))
        again = asyncio.run(auditor.audit(client, _target_of(env, destination), resume=True))
        assert again.resumed_shelves  # updated Kavita and `target recheck`: back in service


@pytest.mark.req("EF-EXP-055", "EF-USR-015")
def test_child_account_of_the_right_tier_does_not_pause_its_family_shelf(
    settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path) as env:
        dedicated_adult_library(env)
        destination = family(env)
        ref = link(env, destination)
        inst = env.inst
        inst.kavita.users.append(member("kid", libraries=(int(ref),), age_rating=8))  # 13+
        target = _target_of(env, destination)
        auditor = AccessAuditor(
            inst.engine, inst.comp.clock, BusTargetEvents(inst.engine, inst.comp.bus)
        )
        report = asyncio.run(auditor.audit(inst.comp.kavita_client(target), target))
        assert report.paused_shelves == []  # the child reads what it is meant to read
        inst.kavita.users.append(member("tiny", libraries=(int(ref),), age_rating=5))  # 10+: below
        report = asyncio.run(auditor.audit(inst.comp.kavita_client(target), target))
        assert report.paused_shelves  # a ceiling above the tier of one account: paused


@pytest.mark.req("EF-EXP-050", "EF-USR-015")
def test_hold_until_rated_for_youth_sub_source(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        dedicated_adult_library(b.env)
        family(b.env)
        b.upstream({1: "One", 2: "Two"})
        b.follow()
        editions = b.rows("SELECT id, shelf_id, rated_at FROM edition")
        assert editions and all(e["shelf_id"] is None for e in editions)  # shelved nowhere
        assert all(e["rated_at"] is None for e in editions)  # "À classer"
        assert b.rows("SELECT count(*) AS n FROM unit")[0]["n"] >= 1  # the chapter arrived
        b.export()
        assert b.env.exported_files() == []  # and went nowhere
        assert b.rows("SELECT hold_until_rated AS h FROM shelf WHERE name = 'Famille'")[0]["h"] == 1


@pytest.mark.req("EF-EXP-050")
def test_without_a_family_shelf_the_follow_files_on_the_adult_shelf_as_before(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        b.upstream({1: "One"})
        b.follow()
        editions = b.rows("SELECT shelf_id FROM edition")
        assert editions and all(e["shelf_id"] is not None for e in editions)


@pytest.mark.req("EF-EXP-046")
def test_unshelved_edition_not_exported_kept_native_in_store(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        dedicated_adult_library(b.env)
        family(b.env)
        b.upstream({1: "One", 2: "Two"})
        b.follow()
        b.export()
        assert b.env.exported_files() == [] and b.rows("SELECT * FROM export_record") == []
        unit_id = b.rows("SELECT id FROM unit ORDER BY id")[0]["id"]
        with b.engine.begin() as connection:  # what a download leaves: received, not yet kept
            variant = connection.execute(
                sa.select(catalog.unit_variant.c.id).where(
                    catalog.unit_variant.c.unit_id == unit_id
                )
            ).scalar_one()
            connection.execute(
                sa.insert(storage.content).values(
                    id=new_ulid(), unit_id=unit_id, variant_id=variant, kind="images",
                    media_type="application/vnd.comicbook+zip", format="cbz",
                    retention="passthrough", path=None, original_sha256="sha256:" + "0" * 64,
                    store_provenance="received", size=1234,
                )
            )  # fmt: skip
        assert [c.unit_id for c in keep_candidates(b.engine)] == [unit_id]  # owed a native copy
        assert b.env.exported_files() == []  # still nowhere


@pytest.mark.req("EF-EXP-048", "EF-USR-015", "EF-BIB-055")
def test_adoption_into_a_family_shelf_suspends_the_unrated_works(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """L11 lifts the refusal of L10: the Work is adopted, nobody rated it, so it waits in "À
    classer" with its following suspended and the file stays exactly where it was."""
    with bench54(monkeypatch, settings, tmp_path) as b:
        dedicated_adult_library(b.env)
        destination = family(b.env)
        ref = link(b.env, destination)
        folder = b.env.inst.library / "Famille" / "Lupin"
        folder.mkdir(parents=True)
        written = cbz_bytes((("1.png", b"\x89PNG\r\n\x1a\nlupin"),))
        (folder / "Lupin v01.cbz").write_bytes(written)
        b.env.inst.kavita.add_series(
            "Lupin", "/manga/Famille/Lupin", ["/manga/Famille/Lupin/Lupin v01.cbz"],
            library_id=int(ref),
        )  # fmt: skip
        base = ("adopt", "--destination", destination, "--library", ref)
        capsys.readouterr()
        assert b.cli(*base, "--dry-run") == 0
        shown = capsys.readouterr().out
        assert "À classer : Lupin" in shown and "suivi suspendu" in shown
        fingerprint = shown.split("aperçu :")[1].split()[0]
        assert b.cli(*base, "--confirm", fingerprint) == 0
        assert (folder / "Lupin v01.cbz").read_bytes() == written  # not touched
        (work,) = b.rows("SELECT id, rated_at, age_rating FROM work")
        assert work["rated_at"] is None and work["age_rating"] is None
        (edition,) = b.rows("SELECT shelf_id, rated_at FROM edition")
        assert edition["rated_at"] is None
        (shelf,) = b.rows("SELECT follow_suspended FROM shelf WHERE id = :s", s=edition["shelf_id"])
        assert work["id"] in shelf["follow_suspended"]
        status = b.status(capsys)
        assert "follow_suspended" in status and "shelfhold rate" in status
