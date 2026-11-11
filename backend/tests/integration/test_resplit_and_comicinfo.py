"""M2 L26 (EF-BIB-044, EF-EXP-040): re-cut chapters and the range read from ComicInfo.

A chapter the source cuts again is a proposal, applied only once confirmed, and removes nothing;
the age of the series is never carried by the proposal: it stays with the Edition and the new
Units leave through the series age barrier of L09 like any other.
"""

from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import (
    ConfirmationRequiredError,
    ResplitError,
    ResplitSigner,
    apply_resplit,
    propose_resplit,
)
from shelfhold.export.application.coverage import (
    CoverageError,
    apply_comicinfo_range,
    comicinfo_range,
)
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from tests.integration.test_schema_catalog import add_edition, add_unit, add_work
from tests.library_env import LibEnv, lib_env
from tests.ranged_env import ranged_bench, signer


@pytest.fixture
def lib() -> Iterator[LibEnv]:
    with lib_env() as env:
        yield env


def counts(lib: LibEnv) -> dict[str, int]:
    with lib.engine.connect() as connection:
        return {
            "coverage": connection.execute(
                sa.select(sa.func.count()).select_from(export.unit_coverage)
            ).scalar_one(),
            "units": connection.execute(
                sa.select(sa.func.count()).select_from(catalog.unit)
            ).scalar_one(),
            "items": connection.execute(
                sa.select(sa.func.count()).select_from(export.export_item)
            ).scalar_one(),
        }


def new_children(lib: LibEnv, parent: str, n: int = 2) -> list[str]:
    """Chapters the source cut `parent` into: its sub-numbers (10 into 10.1 and 10.2)."""
    with lib.engine.begin() as connection:
        number = connection.execute(
            sa.select(catalog.unit.c.number_norm).where(catalog.unit.c.id == parent)
        ).scalar_one()
        return [
            add_unit(connection, lib.chain["edition"], f"{number}.{k}") for k in range(1, n + 1)
        ]


@pytest.mark.req("EF-BIB-044")
def test_resplit_chapter_proposes_coverage_never_removes(lib: LibEnv) -> None:
    bench = ranged_bench(lib, 3)
    parent = bench.unit_ids[1]
    children = new_children(lib, parent)
    before = counts(lib)
    with lib.engine.begin() as connection:
        proposal = propose_resplit(connection, parent, children, signer(lib))
    assert counts(lib) == before  # a proposal writes nothing
    with lib.engine.begin() as connection:
        assert apply_resplit(connection, proposal, signer(lib), confirmation=proposal.token) == 2
    after = counts(lib)
    assert after["coverage"] == before["coverage"] + 2  # added...
    assert after["units"] == before["units"] and after["items"] == before["items"]  # ...no removal
    with lib.engine.connect() as connection:
        cov = export.unit_coverage
        rows = connection.execute(
            sa.select(
                cov.c.carrier_type, cov.c.carrier_id, cov.c.unit_id, cov.c.extent, cov.c.origin
            ).where(cov.c.carrier_type == "unit")
        ).all()
        assert {(r.carrier_id, r.unit_id) for r in rows} == {(parent, c) for c in children}
        assert {r.extent for r in rows} == {"partial"}
        held = connection.execute(  # the old Unit is still carried by its volume
            sa.select(cov.c.carrier_id).where(
                cov.c.carrier_type == "export_item", cov.c.unit_id == parent
            )
        ).scalar_one()
        assert held == bench.item_id
    with lib.engine.begin() as connection:  # again: nothing more
        assert apply_resplit(connection, proposal, signer(lib), confirmation=proposal.token) == 0
    assert counts(lib) == after


@pytest.mark.req("EF-BIB-044")
def test_resplit_proposal_applied_only_when_confirmed(lib: LibEnv) -> None:
    bench = ranged_bench(lib, 2)
    children = new_children(lib, bench.unit_ids[0])
    with lib.engine.begin() as connection:
        proposal = propose_resplit(connection, bench.unit_ids[0], children, signer(lib))
        again = propose_resplit(
            connection, bench.unit_ids[0], children, signer(lib)
        )  # another nonce
    before = counts(lib)
    for wrong in (None, "", "yes", "x" * 24, again.token):
        with lib.engine.begin() as connection, pytest.raises(ConfirmationRequiredError):
            apply_resplit(connection, proposal, signer(lib), confirmation=wrong)
    assert counts(lib) == before


@pytest.mark.req("EF-BIB-044")
def test_resplit_proposal_refuses_what_is_not_one(lib: LibEnv) -> None:
    bench = ranged_bench(lib, 2)
    parent = bench.unit_ids[0]
    children = new_children(lib, parent)
    with lib.engine.begin() as connection:
        for bad_children in ([], [parent], [new_ulid()], [children[0], new_ulid()]):
            with pytest.raises(ResplitError):
                propose_resplit(connection, parent, bad_children, signer(lib))
        with pytest.raises(ResplitError):
            propose_resplit(connection, new_ulid(), children, signer(lib))
        work = add_work(connection)
        foreign = add_unit(connection, add_edition(connection, work), "77")
        unrelated = add_unit(connection, lib.chain["edition"], "99")  # no sub-number of the parent
        with pytest.raises(ResplitError, match="different Editions"):
            propose_resplit(connection, parent, [children[0], foreign], signer(lib))
        with pytest.raises(ResplitError, match="sub-numbers"):
            propose_resplit(connection, parent, [children[0], unrelated], signer(lib))
        with pytest.raises(ResplitError, match="sub-numbers"):  # a sibling is no re-cut
            propose_resplit(connection, parent, [bench.unit_ids[1]], signer(lib))
        with pytest.raises(ResplitError, match="already carried"):  # held by a file already
            propose_resplit(
                connection, parent, [unrelated_carried(lib, connection, parent)], signer(lib)
            )
        with pytest.raises(ResplitError):
            propose_resplit(connection, parent, [new_ulid() for _ in range(201)], signer(lib))
        proposal = propose_resplit(connection, parent, children, signer(lib))
        connection.execute(sa.delete(catalog.unit).where(catalog.unit.c.id == children[0]))
        with pytest.raises(ResplitError, match="does not hold"):  # stale: a child is gone
            apply_resplit(connection, proposal, signer(lib), confirmation=proposal.token)


# -- the range read from ComicInfo -----------------------------------------------------------


def adopted_volume(lib: LibEnv, number: str = "5") -> str:
    item = new_ulid()
    with lib.engine.begin() as connection:
        connection.execute(
            sa.insert(export.export_item).values(
                id=item, edition_id=lib.chain["edition"], kind="adopted",
                frozen_attrs={"kind": "volume", "number_norm": number},
            )
        )  # fmt: skip
    return item


@pytest.mark.req("EF-BIB-044")
def test_volume_range_read_from_comicinfo(lib: LibEnv) -> None:
    item = adopted_volume(lib)
    info = {"Series": "Roman", "Volume": "5", "Number": "41-50"}
    assert comicinfo_range(info) == ("5", "41", "50")
    with lib.engine.begin() as connection:
        assert apply_comicinfo_range(connection, lib.chain["edition"], info) == 10
        cov = export.unit_coverage
        rows = connection.execute(
            sa.select(cov.c.origin, cov.c.carrier_id).where(cov.c.carrier_type == "export_item")
        ).all()
        assert len(rows) == 10 and {r.origin for r in rows} == {"comicinfo"}
        assert {r.carrier_id for r in rows} == {item}
        assert apply_comicinfo_range(connection, lib.chain["edition"], info) == 0  # idempotent


@pytest.mark.req("EF-BIB-044")
@pytest.mark.parametrize(
    "info",
    [
        {},
        {"Volume": "5"},
        {"Number": "41-50"},
        {"Volume": "x", "Number": "41-50"},
        {"Volume": "5", "Number": "50-41"},
        {"Volume": "5", "Number": "41"},
        {"Volume": "5", "Number": "4a-50"},
        {"Volume": "5", "Number": "41-50-60"},
        {"Volume": "", "Number": "1-2"},
        {"Volume": "-5", "Number": "1-2"},
    ],
)
def test_comicinfo_without_a_clear_range_says_nothing(lib: LibEnv, info: dict[str, str]) -> None:
    adopted_volume(lib)
    assert comicinfo_range(info) is None
    before = counts(lib)
    with lib.engine.begin() as connection:
        assert apply_comicinfo_range(connection, lib.chain["edition"], info) == 0
    assert counts(lib) == before  # never a guess


@pytest.mark.req("EF-BIB-044")
def test_comicinfo_range_refuses_absurd_or_taken_chapters(lib: LibEnv) -> None:
    adopted_volume(lib, "5")
    adopted_volume(lib, "6")
    with lib.engine.begin() as connection:
        with pytest.raises(CoverageError):  # 100000 chapters under one volume is a typo
            apply_comicinfo_range(
                connection, lib.chain["edition"], {"Volume": "5", "Number": "1-100000"}
            )
        assert (
            apply_comicinfo_range(
                connection, lib.chain["edition"], {"Volume": "5", "Number": "1-3"}
            )
            == 3
        )
        with pytest.raises(CoverageError, match="already carried"):  # two files never share
            apply_comicinfo_range(
                connection, lib.chain["edition"], {"Volume": "6", "Number": "3-5"}
            )
        with pytest.raises(LookupError):  # no such adopted volume
            apply_comicinfo_range(
                connection, lib.chain["edition"], {"Volume": "9", "Number": "1-2"}
            )


@pytest.mark.req("EF-BIB-044")
def test_comicinfo_read_from_an_adopted_archive_carries_the_volume(lib: LibEnv) -> None:
    from shelfhold.export.targets.kavita.adopt import read_comicinfo
    from tests.library_env import PNG, cbz_bytes

    xml = b"<ComicInfo><Series>Roman</Series><Volume>5</Volume><Number>41-50</Number></ComicInfo>"
    path = lib.manga / "Roman" / "Roman v05.cbz"
    path.parent.mkdir(parents=True)
    path.write_bytes(cbz_bytes((("001.png", PNG),), comicinfo=xml))
    info = read_comicinfo(lib.fsops, str(path))
    assert comicinfo_range(info) == ("5", "41", "50")


# -- the age of the series survives the re-cut: it still goes through the L09 barrier -----------


@pytest.mark.req("EF-BIB-044", "EF-EXP-040")
def test_resplit_never_loses_series_age_and_new_units_wait_for_the_barrier() -> None:
    from shelfhold.export.application.series_age import due_ages
    from shelfhold.infrastructure.schema import catalog as cat
    from tests.epub_export_env import (
        EVERYONE_10,
        TEEN,
        add_book,
        books_library,
        export_round,
        kavita_scans,
        open_gate,
        written,
    )
    from tests.family_env import family_env, rate
    from tests.kavita_export_env import wire

    with family_env(13) as env:
        books_library(env)
        w = wire(env)
        book = env.new_series("Le Livre")
        parent = add_book(env, book, "10")
        rate(env, book, 10)
        assert len(export_round(w)) == 1
        sid = kavita_scans(w, "Le Livre")
        open_gate(w)
        assert w.fake.ages[sid]["ageRating"] == EVERYONE_10

        def age_columns() -> object:
            with env.engine.connect() as connection:
                row = connection.execute(
                    sa.select(cat.edition).where(cat.edition.c.id == book.edition_id)
                ).one()
            return {k: v for k, v in row._mapping.items() if k.startswith("age")}

        before = age_columns()
        first, second = add_book(env, book, "10.1"), add_book(env, book, "10.2")  # the re-cut
        with env.engine.begin() as connection:
            proposal = propose_resplit(connection, parent, [first, second], signer(env.lib))
            applied = apply_resplit(
                connection, proposal, signer(env.lib), confirmation=proposal.token
            )
            assert applied == 2
        assert age_columns() == before  # nothing of the age moved with the coverage
        with env.engine.connect() as connection:
            (task,) = due_ages(connection, env.destination_id)
        assert task.wanted.min_age == 10  # the series still has the age to push

        # reclassified upwards while the new Units wait: nothing leaves until the age is pushed
        rate(env, book, 13)
        w.fake.remove_route("POST", "/api/Series/metadata")
        assert export_round(w) == [] and len(written(env)) == 1
        assert w.fake.ages[sid]["ageRating"] == EVERYONE_10
        blocked = sorted(b.code for b in env.pipeline.preview(env.destination_id).blocked)
        assert blocked == ["export.awaiting_series_age"] * 2
        w.fake.missing.clear()
        w.rebuild_client()
        assert len(export_round(w)) >= 1  # the barrier opens: the age first, then the files
        assert w.fake.ages[sid]["ageRating"] == TEEN and w.fake.ages[sid]["ageRatingLocked"]
        assert w.fake.violations == []


def unrelated_carried(lib: LibEnv, connection: sa.Connection, parent: str) -> str:
    """A sub-number of the parent that another file carries already."""
    number = connection.execute(
        sa.select(catalog.unit.c.number_norm).where(catalog.unit.c.id == parent)
    ).scalar_one()
    child = add_unit(connection, lib.chain["edition"], f"{number}.9")
    connection.execute(
        sa.insert(export.unit_coverage).values(
            carrier_type="export_item", carrier_id=connection.execute(
                sa.select(export.export_item.c.id).limit(1)
            ).scalar_one(), unit_id=child, position=50, extent="full", origin="provider",
        )
    )  # fmt: skip
    return child


@pytest.mark.req("EF-BIB-044")
def test_resplit_token_is_not_forgeable_and_binds_the_coverage_it_saw(lib: LibEnv) -> None:
    import hashlib
    import json
    from dataclasses import replace

    bench = ranged_bench(lib, 3)
    parent = bench.unit_ids[0]
    children = new_children(lib, parent)
    with lib.engine.begin() as connection:
        missing = add_unit(connection, lib.chain["edition"], "99")  # never delivered
    forged_children = (missing, bench.unit_ids[2])  # a chapter no file holds, one already carried
    edition = lib.chain["edition"]
    bare = hashlib.sha256(
        json.dumps([edition, parent, list(forged_children)], separators=(",", ":")).encode()
    ).hexdigest()[:24]
    from shelfhold.catalog.public import ResplitProposal

    before = counts(lib)
    for token in (bare, "0" * 32):
        forged = ResplitProposal(edition, parent, forged_children, "s", "n", token)
        with lib.engine.begin() as connection, pytest.raises(ConfirmationRequiredError):
            apply_resplit(connection, forged, signer(lib), confirmation=token)
    with lib.engine.begin() as connection:
        proposal = propose_resplit(connection, parent, children, signer(lib))
    swapped = replace(proposal, child_unit_ids=(*proposal.child_unit_ids, missing))
    with lib.engine.begin() as connection, pytest.raises(ConfirmationRequiredError):
        apply_resplit(connection, swapped, signer(lib), confirmation=proposal.token)
    assert counts(lib) == before
    with lib.engine.begin() as connection:  # the coverage moved since the proposal: stale
        connection.execute(
            sa.insert(export.unit_coverage).values(
                carrier_type="export_item", carrier_id=bench.item_id, unit_id=children[0],
                position=60, extent="full", origin="manual",
            )
        )  # fmt: skip
    with lib.engine.begin() as connection, pytest.raises(ResplitError):
        apply_resplit(connection, proposal, signer(lib), confirmation=proposal.token)


@pytest.mark.req("EF-BIB-044")
def test_resplit_proposed_then_confirmed_after_a_restart_or_in_another_process(
    lib: LibEnv,
) -> None:
    bench = ranged_bench(lib, 2)
    parent = bench.unit_ids[0]
    children = new_children(lib, parent)
    with lib.engine.begin() as connection:
        proposal = propose_resplit(connection, parent, children, signer(lib))  # the API
    assert (lib.data / "resplit.key").stat().st_mode & 0o077 == 0  # owner only
    with lib.engine.begin() as connection:  # the CLI, or the same service after a restart
        assert apply_resplit(connection, proposal, signer(lib), confirmation=proposal.token) == 2
    # another instance (another /data, so another key) does not accept it
    other = ResplitSigner(lib.tmp / "elsewhere")
    (lib.tmp / "elsewhere").mkdir()
    with lib.engine.begin() as connection, pytest.raises(ConfirmationRequiredError):
        apply_resplit(connection, proposal, other, confirmation=proposal.token)
