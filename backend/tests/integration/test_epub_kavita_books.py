"""M2 L09 (EF-EXP-040, EF-EXP-033, EF-BIB-001): EPUB series in a Kavita Books library.

No age is written in an EPUB (D1, L08): the age lives in the database and reaches Kavita by its
API, pushed locked (`field_owner`). The first EPUB of a series is exported only while Kavita
answers on a release at or above the floor, and nothing more of the series is exported until its
age has been pushed and read back locked after the scan; a Kavita that fails, does not answer or
drops the lock exports nothing more (fail closed). An `omnibus` Edition is a Kavita series of its
own, with its own age. The pipeline, the database and the files are the production ones; Kavita is
the simulated server (no network).
"""

import json
import os
import zipfile
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.domain.planner import (
    DestinationFacts,
    EditionFacts,
    PlanRequest,
    UnitFacts,
    plan_destination,
)
from shelfhold.export.targets.kavita.arrival import ArrivalPoller
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.library.adapters.origin_opf import origin_opf_rel
from tests.epub_export_env import (
    EVERYONE_10,
    TEEN,
    add_book,
    books_library,
    export_round,
    kavita_scans,
    open_gate,
    visible_to_restricted,
    written,
)
from tests.export_env import ExportEnv, Series, export_env
from tests.fakes.kavita.plugin_parse import simulate_parse
from tests.family_env import family_env, rate, rate_edition
from tests.integration.test_schema_catalog import add_binding, add_edition, add_source_item
from tests.kavita_env import run
from tests.kavita_export_env import Wired, wire

GOLDEN = Path(__file__).parents[1] / "golden" / "kavita_naming" / "omnibus_series.json"


def metadata_posts(w: Wired) -> list[Any]:
    return [r for r in w.fake.requests_to("/Series/metadata") if r.method == "POST"]


def blocked_codes(env: ExportEnv) -> list[str]:
    preview = env.pipeline.preview(env.destination_id)
    return sorted(b.code for b in preview.blocked)


@pytest.mark.req("EF-EXP-040", "EF-EXP-033")
def test_epub_series_age_pushed_locked_visible_to_restricted_child() -> None:
    """Given an EPUB series rated 10+ on the family shelf and a child account restricted to 13+
    without "include unknowns", when it is exported then scanned, its series age is locked at the
    rated value and the child sees it; nothing of the age is in the file."""
    with family_env(13) as env:
        books_library(env)
        w = wire(env)
        book = env.new_series("Le Livre")
        unit = add_book(env, book, "1")
        rate(env, book, 10)

        assert len(export_round(w)) == 1
        sid = kavita_scans(w, "Le Livre")
        assert w.fake.ages[sid]["ageRating"] == 0  # Kavita reads no age in an EPUB
        assert not visible_to_restricted(0, ceiling=TEEN, include_unknowns=False)

        facts = open_gate(w)  # pushed, then read back after the scan
        assert w.fake.ages[sid]["ageRating"] == EVERYONE_10
        assert w.fake.ages[sid]["ageRatingLocked"] is True
        assert visible_to_restricted(EVERYONE_10, ceiling=TEEN, include_unknowns=False)
        (series,) = env.rows(export.export_series)
        assert series.id in facts.verified
        with env.engine.connect() as connection:
            owned = connection.execute(sa.select(export.field_owner)).one()
        assert (owned.field, owned.last_value) == ("age_rating", EVERYONE_10)
        assert owned.pushed_at is not None

        # the file is the passthrough of L08: no age anywhere, the original OPF in /data/meta
        (path,) = env.files()
        with zipfile.ZipFile(path) as archive:
            opf = next(n for n in archive.namelist() if n.endswith(".opf"))
            text = archive.read(opf).decode().lower()
        assert "shelfhold" not in text and "agerating" not in text and "10+" not in text
        (record,) = env.rows(export.export_record)
        assert record.format == "epub" and path.suffix == ".epub"
        assert (env.lib.data / origin_opf_rel(unit)).is_file()

        rate(env, book, 13)  # reclassified in the same shelf: pushed again, locked
        open_gate(w)
        assert w.fake.ages[sid]["ageRating"] == TEEN and w.fake.ages[sid]["ageRatingLocked"]
        assert visible_to_restricted(TEEN, ceiling=TEEN, include_unknowns=False)
        assert len(metadata_posts(w)) == 2
        assert w.fake.violations == []


@pytest.mark.req("EF-EXP-040", "EF-EXP-033")
def test_first_epub_export_waits_for_age_push() -> None:
    """The first EPUB of a series goes alone; the rest waits until the age is pushed and read
    back locked after the scan. Kavita down, failing or dropping the lock: nothing more leaves."""
    with family_env(13) as env:
        books_library(env)
        w = wire(env)
        book = env.new_series("Le Livre")
        add_book(env, book, "1")
        add_book(env, book, "2")
        rate(env, book, 10)

        # Kavita does not answer: the gate is closed, not one file is written
        w.fake.asleep = True
        assert export_round(w) == [] and env.files() == []
        assert blocked_codes(env) == ["export.awaiting_series_age"] * 2
        w.fake.wake_up()

        # Kavita answers: the first book alone, the second waits for the age
        (first,) = export_round(w)
        assert written(env) == [first]
        assert export_round(w) == []  # not scanned yet: no age could be pushed
        sid = kavita_scans(w, "Le Livre")

        # the push fails: nothing more leaves
        w.fake.remove_route("POST", "/api/Series/metadata")
        assert export_round(w) == [] and len(written(env)) == 1
        w.fake.missing.clear()
        w.rebuild_client()  # Kavita upgraded and restarted: the route is back

        # Kavita takes the push but drops the lock: read back unlocked, nothing more leaves
        w.fake.drops_age_lock = True
        assert export_round(w) == [] and len(written(env)) == 1
        assert w.fake.ages[sid]["ageRatingLocked"] is False
        w.fake.drops_age_lock = False

        # the age is pushed and read back locked: only then the second book
        posts = len(metadata_posts(w))
        (second,) = export_round(w)
        assert len(metadata_posts(w)) == posts + 1
        assert w.fake.ages[sid]["ageRatingLocked"] is True
        assert w.fake.ages[sid]["ageRating"] == EVERYONE_10
        assert written(env) == sorted([first, second])

        # reclassified upwards with books already exported: the age is pushed before any new
        # book of the series is exposed; a push Kavita refuses stops the series
        add_book(env, book, "3")
        rate(env, book, 13)
        w.fake.remove_route("POST", "/api/Series/metadata")
        assert export_round(w) == [] and len(written(env)) == 2
        assert w.fake.ages[sid]["ageRating"] == EVERYONE_10
        w.fake.missing.clear()
        w.rebuild_client()
        (third,) = export_round(w)
        assert w.fake.ages[sid]["ageRating"] == TEEN
        assert len(written(env)) == 3 and third in written(env)
        assert w.fake.violations == []


@pytest.mark.req("EF-EXP-040")
def test_names_are_asked_for_a_first_epub_before_the_gate_is_read() -> None:
    """The job reads the age gate from Kavita only when a name is asked: the question is put with
    the gate open, or a rated EPUB would ask no name and the gate would never be read."""
    with family_env(13) as env:
        books_library(env)
        book = env.new_series("Le Livre")
        add_book(env, book, "1")
        rate(env, book, 10)
        names, _kind = env.pipeline.wanted_names(env.destination_id)
        assert names != []


@pytest.mark.req("EF-BIB-001", "EF-EXP-040")
def test_omnibus_packaging_separate_edition_two_kavita_series() -> None:
    """Golden: an `omnibus` Edition of the same Work, in the same language, is a Kavita series of
    its own (its own name, folder and age), never mixed with the Edition it repackages."""
    with family_env(13) as env:
        books_library(env)
        w = wire(env)
        book = env.new_series("Le Livre")
        with env.engine.begin() as connection:
            omnibus = add_edition(
                connection, book.work_id, lang="fr", packaging="omnibus", shelf_id=env.shelf_id
            )
            item = add_source_item(connection, remote_id="series-omnibus")
            binding = add_binding(connection, book.work_id, omnibus, item)
        bundle = Series(book.work_id, omnibus, binding)
        add_book(env, book, "1")
        add_book(env, bundle, "1")
        rate(env, book, 13)
        rate_edition(env, omnibus, 10)  # the omnibus has an age of its own

        assert len(export_round(w)) == 2
        series = _series_folders(env)
        assert len(series) == 2 and len({s["normalized"] for s in series}) == 2
        assert len({s["folder"] for s in series}) == 2
        for entry in series:
            files = [w.remote(r) for r in written(env) if r.startswith(entry["folder"] + "/")]
            w.fake.discover(f"/manga/Kavita/{entry['folder']}", files)
        run(ArrivalPoller(env.engine, env.clock, w.events).poll(w.client, env.destination_id))
        open_gate(w)
        by_folder = {s["folderPath"]: s["id"] for s in w.fake.series}
        found = [
            {
                "name": s["name"],
                "folder": s["folder"],
                "proposal_key": s["proposal_key"],
                "age_rating": w.fake.ages[by_folder[f"/manga/Kavita/{s['folder']}"]]["ageRating"],
            }
            for s in series
        ]
        if os.environ.get("SHELFHOLD_UPDATE_GOLDEN") == "1":
            GOLDEN.write_text(json.dumps(found, ensure_ascii=False, indent=2) + "\n")
        assert json.loads(GOLDEN.read_text()) == found
        assert {f["age_rating"] for f in found} == {EVERYONE_10, TEEN}  # one age per series


def _series_folders(env: ExportEnv) -> list[dict[str, Any]]:
    """Each exported series: its name, its folder and the rule that proposed it (the Work id,
    an identifier made by the test, is left out of the golden)."""
    es, link = export.export_series, export.edition_export_series
    item, record = export.export_item, export.export_record
    with env.engine.connect() as connection:
        rows = connection.execute(
            sa.select(es.c.name_raw, es.c.normalized_name, es.c.proposal_key, record.c.rel_path)
            .select_from(
                es.join(link, link.c.export_series_id == es.c.id)
                .join(item, item.c.edition_id == link.c.edition_id)
                .join(record, record.c.export_item_id == item.c.id)
            )
            .order_by(es.c.name_raw)
        ).all()
    return [
        {
            "name": r.name_raw,
            "normalized": r.normalized_name,
            "folder": str(r.rel_path).rsplit("/", 1)[0],
            "proposal_key": {k: v for k, v in (r.proposal_key or {}).items() if k != "work"},
        }
        for r in rows
    ]


def _facts(library_type: str | None) -> PlanRequest:
    edition = EditionFacts("ed-1", "work-1", "Le Livre", "fr")
    unit = UnitFacts("unit-1", "ed-1", "1", content_hash="sha256:x", format="epub")
    return PlanRequest(
        destination=DestinationFacts(
            "dest-1", "shelf-1", remote_library_id="lib-1", library_type=library_type
        ),
        editions=(edition,),
        units=(unit,),
        holds_books=True,
    )


@pytest.mark.req("EF-EXP-040")
def test_novel_shelf_requires_book_library_type() -> None:
    """D19: a shelf of `novel` Works aims at a Kavita library of type Book; any other type is
    announced (`target.library_type_mismatch`), Shelfhold never creates nor retypes a library."""
    for kind in ("manga", "comic", None):
        plan = plan_destination(_facts(kind), simulate_parse)
        assert "target.library_type_mismatch" in [w.code for w in plan.warnings]
    plan = plan_destination(_facts("book"), simulate_parse)
    assert "target.library_type_mismatch" not in [w.code for w in plan.warnings]

    # through the loader: Works of kind `novel` filed on a shelf whose library is a Manga one
    with export_env() as env:
        book = env.new_series("Le Roman")
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(catalog.work).where(catalog.work.c.id == book.work_id)
                .values(kind="novel")
            )  # fmt: skip
        add_book(env, book, "1")
        preview = env.pipeline.preview(env.destination_id)
        assert "target.library_type_mismatch" in [w.code for w in preview.warnings]
        books_library(env)
        preview = env.pipeline.preview(env.destination_id)
        assert "target.library_type_mismatch" not in [w.code for w in preview.warnings]
