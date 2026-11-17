"""M4 L10 (EF-EXP-019, EF-BIB-044, I20): chapters served one by one become one volume.

`replace(old_items[], new_item)` under one intent: the volume is written, each chapter file goes to
the bin of its root (never unlinked), and the manifest, the coverage and the aliases say the same
in one transaction. The volume is a derivative: it is never counted as an independent copy of its
chapters, and the progress of each member survives it.
"""

import io
import json
import zipfile

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import present_in
from shelfhold.export.application.assembly import AssemblyOutcome
from shelfhold.export.application.intents import open_intent_paths
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import (
    catalog,
    coherence,
    export,
    platform,
    replica,
    storage,
)
from shelfhold.library.public import independent_copies
from shelfhold.ops.status import koreader_reset_report
from tests.assembly_env import (
    bin_files,
    digest,
    exported_chapters,
    page_bytes,
    pages_of,
    position,
    progress_of,
    reader,
    request,
    service,
)
from tests.export_env import export_env
from tests.family_env import family_env, rate, rate_edition

pytestmark = pytest.mark.req("EF-EXP-019", "EF-BIB-044")


def _old_pages(chapters: object) -> list[bytes]:
    pages: list[bytes] = []
    for body in chapters.old_bytes:  # type: ignore[attr-defined]
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            pages += [archive.read(n) for n in archive.namelist() if n != "ComicInfo.xml"]
    return pages


@pytest.mark.req("EF-EXP-019")
def test_volume_format_and_webtoon_assembly_replace_old_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ten chapters of a tome become one CBZ, which replaces the ten entries of the manifest: here
    three long chapters of a webtoon. Nothing is unlinked: the old files are in the bin."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an assembly never unlinks a file")

    monkeypatch.setattr(Fsops, "unlink", refuse)
    with export_env() as env:
        chapters = exported_chapters(env, [4, 3, 5])
        scans: list[str] = []

        result = service(env, scans=scans).assemble(request(env, chapters))

        assert result.outcome is AssemblyOutcome.ASSEMBLED
        (volume,) = env.files()  # one file served where three were
        assert volume not in chapters.targets
        names = pages_of(volume)
        assert names[-1] == "ComicInfo.xml" and len(names) == 12 + 1  # every page, ComicInfo last
        assert page_bytes(volume) == _old_pages(chapters)  # the reading order of the chapters
        assert sorted(p.read_bytes() for p in bin_files(env)) == sorted(chapters.old_bytes)
        record, item = export.export_record, export.export_item
        with env.engine.connect() as connection:
            lines = {
                str(r.export_item_id): r
                for r in connection.execute(
                    sa.select(record).where(record.c.shelf_destination_id == env.destination_id)
                )
            }
            assert open_intent_paths(connection) == frozenset()
            intents = connection.execute(
                sa.select(coherence.fs_intent.c.op, coherence.fs_intent.c.state)
            ).all()
            new_id = result.export_item_id
            kind = connection.execute(
                sa.select(item.c.kind, item.c.content_id).where(item.c.id == new_id)
            ).one()
            assemblies = connection.execute(sa.select(export.export_assembly)).all()
        assert [lines[i].state for i in chapters.items] == ["recycled"] * 3
        assert lines[new_id].state == "written"
        assert lines[new_id].file_hash == digest(volume)
        assert str(volume).endswith(str(lines[new_id].rel_path))
        assert [(i.op, i.state) for i in intents if i.op == "assemble"] == [("assemble", "done")]
        assert kind.kind == "volume" and kind.content_id is None  # no content was received for it
        (assembly,) = assemblies
        assert assembly.export_item_id == new_id
        assert json.loads(json.dumps(assembly.old_items))  # the entries it replaced, listed
        assert scans  # Kavita is asked to look at the folder again


@pytest.mark.req("EF-BIB-044")
def test_volume_assembly_recorded_in_unit_coverage() -> None:
    """The coverage of the assembled volume is in `unit_coverage` (I20): every chapter is present
    through the volume, with its bounds in it, and the pipeline does not export them again."""
    with export_env() as env:
        chapters = exported_chapters(env, [4, 3, 5])

        result = service(env).assemble(request(env, chapters))

        cover = export.unit_coverage
        with env.engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    cover.c.unit_id,
                    cover.c.position,
                    sa.func.json_extract(cover.c.start_locator, "$.page").label("start"),
                    sa.func.json_extract(cover.c.end_locator, "$.page").label("end"),
                    cover.c.extent,
                )
                .where(
                    cover.c.carrier_type == "export_item",
                    cover.c.carrier_id == result.export_item_id,
                )
                .order_by(cover.c.position)
            ).all()
            present = [present_in(connection, u, env.destination_id) for u in chapters.units]
            live = [
                connection.execute(
                    sa.text(
                        "SELECT count(*) FROM export_record r JOIN unit_coverage c "
                        "ON c.carrier_type = 'export_item' AND c.carrier_id = r.export_item_id "
                        "WHERE c.unit_id = :u AND r.state IN "
                        "('written', 'scanned', 'resolved', 'adopted')"
                    ),
                    {"u": u},
                ).scalar_one()
                for u in chapters.units
            ]
        assert [r.unit_id for r in rows] == chapters.units
        assert [(r.position, r.start, r.end) for r in rows] == [(1, 1, 4), (2, 5, 7), (3, 8, 12)]
        assert {r.extent for r in rows} == {"full"}
        assert present == [True] * 3
        assert live == [1, 1, 1]  # one live file per chapter: the volume's, never two
        before = (env.files(), len(env.rows(export.export_record)))

        env.pipeline.run(env.destination_id, job_id="01HZZZZZZZZZZZZZZZZZZZZZZZ")

        assert (env.files(), len(env.rows(export.export_record))) == before  # nothing rewritten


def test_the_volume_is_a_derivative_never_an_independent_copy_of_its_chapters() -> None:
    with export_env() as env:
        chapters = exported_chapters(env, [4, 3])
        with env.engine.connect() as connection:
            before = [independent_copies(connection, u) for u in chapters.units]

        result = service(env).assemble(request(env, chapters))

        with env.engine.connect() as connection:
            after = [independent_copies(connection, u) for u in chapters.units]
            copies = connection.execute(
                sa.select(sa.func.count())
                .select_from(replica.replica)
                .where(replica.replica.c.export_item_id == result.export_item_id)
            ).scalar_one()
            old_copies = connection.execute(
                sa.select(sa.func.count())
                .select_from(replica.replica)
                .where(
                    replica.replica.c.export_item_id.in_(chapters.items),
                    replica.replica.c.location == "destination",
                )
            ).scalar_one()
            volume_hash = connection.execute(
                sa.select(export.export_record.c.content_hash).where(
                    export.export_record.c.export_item_id == result.export_item_id
                )
            ).scalar_one()
        assert after == before  # what held the chapters before still holds them, nothing more
        assert copies == 0  # no replica is told for the volume
        assert old_copies == 0  # the files of the chapters left the destination
        assert volume_hash not in {str(r.content_hash) for r in env.rows(storage.content)}


def test_the_progress_of_each_member_is_kept_without_a_false_read() -> None:
    with export_env() as env:
        chapters = exported_chapters(env, [4, 3, 5])
        ana, bob = reader(env, "ana"), reader(env, "bob")
        # ana was reading the second chapter of the tome, on its own file
        position(
            env,
            ana,
            chapters.items[1],
            chapters.content_hashes[1],
            [chapters.units[1]],
            2,
            percent=0.5,
        )
        before = progress_of(env, ana)

        result = service(env).assemble(request(env, chapters))

        assert before == {chapters.units[1]: "in_progress"}
        after = progress_of(env, ana)
        # what she had is kept, and the profile is told where she is in the volume: no chapter is
        # read that was not (a device report never marks one)
        assert after[chapters.units[1]] == "in_progress" and "read" not in after.values()
        # bob's e-reader now reports a page of the volume: it falls in the second chapter
        assert result.export_item_id is not None
        with env.engine.connect() as connection:
            anchor = connection.execute(  # the hash KOReader's relay anchors a position on
                sa.select(export.document_alias.c.content_hash).where(
                    export.document_alias.c.export_item_id == result.export_item_id
                )
            ).scalar_one()
        position(env, bob, result.export_item_id, anchor, chapters.units, 6, percent=0.5)
        seen = progress_of(env, bob)
        with env.engine.connect() as connection:
            exact = connection.execute(
                sa.text(
                    "SELECT approximate FROM reading_progress WHERE user_id = :u AND unit_id = :c"
                ),
                {"u": bob, "c": chapters.units[1]},
            ).scalar_one()
        assert exact == 0  # page 6 of the volume is the 2nd page of chapter 2, by its bounds
        assert seen[chapters.units[1]] == "in_progress"
        assert chapters.units[0] not in seen or seen[chapters.units[0]] != "read"
        assert chapters.units[2] not in seen or seen[chapters.units[2]] != "read"
        with env.engine.connect() as connection:
            kept = connection.execute(
                sa.text("SELECT count(*) FROM reading_position WHERE carrier_id = :i"),
                {"i": chapters.items[1]},
            ).scalar_one()
        assert kept == 1  # the position written on the old file is still there


def test_the_hash_of_the_volume_resolves_it_and_the_old_ones_still_resolve_the_chapters() -> None:
    """KOReader identifies a document by a partial MD5 of its file: the volume has another one."""
    with export_env() as env:
        chapters = exported_chapters(env, [4, 3])
        with env.engine.connect() as connection:
            before = {
                str(r.export_item_id): str(r.file_hash)
                for r in connection.execute(sa.select(export.document_alias))
            }

        result = service(env).assemble(request(env, chapters))

        (volume,) = env.files()
        alias = export.document_alias
        with env.engine.connect() as connection:
            rows = connection.execute(sa.select(alias)).all()
            reset = koreader_reset_report(connection, env.clock.now().replace(year=2000))
        mine = [r for r in rows if r.export_item_id == result.export_item_id]
        assert [r.file_hash for r in mine] == [digest(volume)]
        assert mine[0].koreader_partial_md5 is not None and mine[0].kavita_md5 is not None
        for item, file_hash in before.items():  # a position taken on a chapter file still resolves
            assert any(r.export_item_id == item and r.file_hash == file_hash for r in rows)
        assert {str(e["unit_id"]) for e in reset} == set(chapters.units)  # `status` says it (D21)
        with env.engine.connect() as connection:
            events = connection.execute(
                sa.select(platform.event.c.payload).where(platform.event.c.type == "unit.upgraded")
            ).all()
        assert {e.payload["reason"] for e in events} == {"volume_assembly"}
        assert all(e.payload["koreader_reset"] is True for e in events)


def test_chapters_of_two_editions_are_never_assembled() -> None:
    """A volume carries one rating and one shelf: two Editions are two files."""
    with export_env() as env:
        first = exported_chapters(env, [2])
        other = exported_chapters(env, [3], series=env.new_series("Other"), first=1)
        items = [first.items[0], other.items[0]]
        before = env.files()

        result = service(env).assemble(request(env, first, items=items))

        assert result.outcome is AssemblyOutcome.MIXED_EDITIONS
        assert env.files() == before and bin_files(env) == []


def test_an_item_that_is_not_a_chapter_is_refused() -> None:
    with export_env() as env:
        chapters = exported_chapters(env, [2, 2])
        first = service(env).assemble(request(env, chapters))
        assert first.outcome is AssemblyOutcome.ASSEMBLED

        volume_item = first.export_item_id
        assert volume_item is not None

        again = service(env).assemble(
            request(env, chapters, items=[volume_item, chapters.items[0]])
        )

        assert again.outcome is AssemblyOutcome.NOT_CHAPTERS  # a volume is not assembled again
        assert len(env.files()) == 1


def test_an_item_of_another_destination_or_a_recycled_one_is_not_served() -> None:
    """IDOR: the items are the destination's own and still served; any other is refused whole."""
    with export_env() as env:
        chapters = exported_chapters(env, [2, 2, 2])
        service(env).assemble(request(env, chapters, items=chapters.items[:2]))
        before = env.files()

        gone = service(env).assemble(request(env, chapters, items=chapters.items[1:]))

        assert gone.outcome is AssemblyOutcome.NOT_SERVED  # the second one is in the bin now
        assert env.files() == before
        stranger = service(env).assemble(
            request(env, chapters, items=["01HZZZZZZZZZZZZZZZZZZZZZZZ", chapters.items[2]])
        )
        assert stranger.outcome is AssemblyOutcome.NOT_SERVED
        assert env.files() == before


def test_fewer_than_two_chapters_make_no_volume() -> None:
    with export_env() as env:
        chapters = exported_chapters(env, [2])

        result = service(env).assemble(request(env, chapters))

        assert result.outcome is AssemblyOutcome.TOO_FEW
        assert env.files() == chapters.targets


@pytest.mark.parametrize("volume", ["", "x", "1/2", "../1", "-3"])
def test_a_volume_number_that_is_not_one_is_refused(volume: str) -> None:
    with export_env() as env:
        chapters = exported_chapters(env, [2, 2])

        result = service(env).assemble(request(env, chapters, volume=volume))

        assert result.outcome is AssemblyOutcome.INVALID_VOLUME
        assert env.files() == chapters.targets


def test_a_volume_name_already_served_is_never_overwritten() -> None:
    with export_env() as env:
        chapters = exported_chapters(env, [2, 2, 2, 2])
        first = service(env).assemble(request(env, chapters, items=chapters.items[:2]))
        assert first.outcome is AssemblyOutcome.ASSEMBLED
        (volume,) = [p for p in env.files() if p not in chapters.targets]
        served = volume.read_bytes()

        again = service(env).assemble(request(env, chapters, items=chapters.items[2:]))

        assert again.outcome is AssemblyOutcome.NAME_TAKEN
        assert volume.read_bytes() == served
        assert len(env.files()) == 3  # the volume and the two chapters left


def test_a_chapter_rewritten_by_a_third_party_stops_the_assembly() -> None:
    with export_env() as env:
        chapters = exported_chapters(env, [2, 2])
        chapters.targets[1].write_bytes(b"PK\x03\x04 rewritten by someone else")

        result = service(env).assemble(request(env, chapters))

        assert result.outcome is AssemblyOutcome.TARGET_CHANGED
        assert bin_files(env) == [] and len(env.files()) == 2


def test_a_frozen_root_writes_nothing() -> None:
    with export_env() as env:
        chapters = exported_chapters(env, [2, 2])
        env.lib.fsops.freeze()

        result = service(env).assemble(request(env, chapters))

        assert result.outcome is AssemblyOutcome.ROOT_FROZEN
        with env.engine.connect() as connection:
            announced = connection.execute(
                sa.select(sa.func.count())
                .select_from(coherence.fs_intent)
                .where(coherence.fs_intent.c.op == "assemble")
            ).scalar_one()
        assert announced == 0  # not even the intent


@pytest.mark.req("EF-BIB-037", "EF-BIB-055")
@pytest.mark.parametrize("how", ["work_raised", "edition_rated_above", "withdrawn"])
def test_a_child_shelf_never_receives_what_it_no_longer_admits_in_a_volume(how: str) -> None:
    """A shelf below 18+ keeps its ceiling: the Edition the chapters came from is rated above it
    (or is no longer filed there), so no volume is made of them and nothing moves."""
    with family_env(13) as env:
        series = env.new_series()
        rate(env, series, 10)
        chapters = exported_chapters(env, [2, 2], series=series)
        before = (env.files(), [p.read_bytes() for p in env.files()])
        if how == "work_raised":
            rate(env, series, 16)
        elif how == "edition_rated_above":
            rate_edition(env, series.edition_id, 16)
        else:
            with env.engine.begin() as connection:
                connection.execute(
                    sa.update(catalog.edition)
                    .where(catalog.edition.c.id == series.edition_id)
                    .values(shelf_id=None)
                )

        result = service(env).assemble(request(env, chapters))

        assert result.outcome is AssemblyOutcome.REFUSED_AGE
        assert (env.files(), [p.read_bytes() for p in env.files()]) == before
        assert bin_files(env) == []


@pytest.mark.req("EF-BIB-037")
def test_the_volume_of_a_child_shelf_takes_the_rating_of_its_edition_and_no_other() -> None:
    with family_env(13) as env:
        series = env.new_series()
        rate(env, series, 10)
        chapters = exported_chapters(env, [2, 2], series=series)

        result = service(env).assemble(request(env, chapters))

        assert result.outcome is AssemblyOutcome.ASSEMBLED
        (volume,) = env.files()
        with zipfile.ZipFile(io.BytesIO(volume.read_bytes())) as archive:
            info = archive.read("ComicInfo.xml").decode()
        assert "<AgeRating>" in info  # the same rating the chapters carried, never none
        with env.engine.connect() as connection:
            editions = connection.execute(
                sa.select(export.export_item.c.edition_id).where(
                    export.export_item.c.id == result.export_item_id
                )
            ).scalar_one()
        assert editions == series.edition_id


def test_a_destination_paused_or_read_only_is_not_written() -> None:
    with export_env() as env:
        chapters = exported_chapters(env, [2, 2])
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(export.shelf)
                .where(export.shelf.c.id == env.shelf_id)
                .values(paused_reason="ceiling_changed")
            )

        result = service(env).assemble(request(env, chapters))

        assert result.outcome is AssemblyOutcome.REFUSED_DESTINATION
        assert bin_files(env) == [] and len(env.files()) == 2


@pytest.mark.req("EF-BIB-037", "ENF-SEC-010")
def test_a_child_below_the_rating_neither_reads_nor_writes_a_position_on_the_volume() -> None:
    """The volume carries the rating of its Edition and nothing else: a child whose ceiling is
    below it gets the answer of a file that is not there, on the volume as on its chapters, and a
    member of the household gets the position of nobody else."""
    from shelfhold.reading.application.progress import UnknownCarrierError, record_position

    with export_env() as env:
        series = env.new_series()
        rate(env, series, 16)
        chapters = exported_chapters(env, [4, 3], series=series)
        result = service(env).assemble(request(env, chapters))
        assert result.export_item_id is not None
        kid = reader(env, "kid", "child", max_age_rating=10)
        adult = reader(env, "adult")
        with env.engine.connect() as connection:
            anchor = connection.execute(
                sa.select(export.document_alias.c.content_hash).where(
                    export.document_alias.c.export_item_id == result.export_item_id
                )
            ).scalar_one()

        def write(reader_id: str, subject_id: str, carrier: str) -> str:
            with write_transaction(env.engine) as connection:
                return record_position(
                    connection, reader_id=reader_id, subject_id=subject_id,
                    carrier_type="export_item", carrier_id=carrier, content_hash=anchor,
                    locator={"page": 2}, percent=0.3, client_ts=env.clock.now(),
                    now=env.clock.now(),
                )  # fmt: skip

        for carrier in (result.export_item_id, *chapters.items):
            with pytest.raises(UnknownCarrierError):  # the child's ceiling is below the Edition's
                write(kid, kid, carrier)
        assert write(adult, adult, result.export_item_id)  # the adult reads on
        with pytest.raises(UnknownCarrierError):  # and nobody writes the position of another
            write(adult, kid, result.export_item_id)
        assert progress_of(env, kid) == {}


def test_the_e_reader_opens_the_volume_where_the_reader_was() -> None:
    """KOReader takes the volume for a new document and asks the relay where it was: the profile
    that was reading the chapters is told the page of the volume, and nothing is marked read."""
    from shelfhold.infrastructure.secrets.keyring import Keyring
    from shelfhold.reading.application.kosync import Kosync

    with export_env() as env:
        chapters = exported_chapters(env, [4, 3, 5])
        ana, bob, cleo = reader(env, "ana"), reader(env, "bob"), reader(env, "cleo")
        # ana is on the 2nd page of chapter 2; bob finished chapter 1 (a read mark kept by Kavita);
        # cleo read nothing
        position(
            env,
            ana,
            chapters.items[1],
            chapters.old_hashes[1],
            [chapters.units[1]],
            2,
            percent=0.5,
        )
        position(
            env,
            bob,
            chapters.items[0],
            chapters.old_hashes[0],
            [chapters.units[0]],
            4,
            percent=1.0,
            source="server",
        )
        before = {u: progress_of(env, u) for u in (ana, bob, cleo)}

        result = service(env).assemble(request(env, chapters))

        with env.engine.connect() as connection:
            alias = connection.execute(
                sa.select(export.document_alias.c.koreader_partial_md5).where(
                    export.document_alias.c.export_item_id == result.export_item_id
                )
            ).scalar_one()
        relay = Kosync(env.engine, lambda: Keyring(b"\x01" * 32), env.clock.now)
        told = {u: relay.pull(user_id=u, document=str(alias).removeprefix("md5:")) for u in before}

        assert (
            told[ana] is not None and told[ana].progress == "6"
        )  # chapter 2 starts at 5: 5 + 2 - 1
        assert told[bob] is not None and told[bob].progress == "4"  # the end of chapter 1
        assert told[cleo] is None  # a profile that read nothing is told nothing
        for user, was in before.items():  # the carry marks nothing read
            now = progress_of(env, user)
            assert {u for u, state in now.items() if state == "read"} <= {
                u for u, state in was.items() if state == "read"
            }
