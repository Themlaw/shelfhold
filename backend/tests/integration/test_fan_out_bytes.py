"""EF-BIB-045, EF-BIB-048, EF-EXP-046, EF-EXP-058 (M4 L06b, intouchable : intégrité).

The bytes of a fan-out come from the registry of copies: `/store`, the library, then a file a
binding wrote when its hash is the one of the manifest and its server can read a copy back;
otherwise a new download is asked for. A file a destination rendered is never the source of
another binding's file: the rendering is made again from the canonical archive. The format is the
first of `accepted_content_types` the Variant has, per binding.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa
from shelfhold_sdk.export_target import TargetCapabilities
from shelfhold_sdk.fsops import ContentRef

from shelfhold.export.application.fan_out import (
    ContentNeedsDownloadError,
    EpubFile,
    FanOut,
    HostContentStore,
    native_formats,
    planned_content,
)
from shelfhold.export.application.plan_executor import PlanRefusedError
from shelfhold.export.domain.planner import (
    NO_NATIVE_FORMAT,
    ContentOffer,
    choose_content,
)
from shelfhold.export.targets.kavita.destination import KavitaDestination
from shelfhold.infrastructure.db import write_transaction
from shelfhold.library.public import independent_copies
from tests.byte_source_env import (
    add_format,
    drop_library_copy,
    inode,
    other_target,
    put_destination_copy,
    put_store_copy,
    unit_with_item,
)
from tests.export_env import ExportEnv, export_env
from tests.fan_out_env import bind
from tests.integration.test_fan_out_plans import Plans, setup, write_plan

BASE = KavitaDestination.capabilities


def caps(readback: str = "none", accepted: tuple[str, ...] = ("cbz", "epub")) -> TargetCapabilities:
    return BASE.model_copy(update={"readback": readback, "accepted_content_types": list(accepted)})


class Registry:
    """What each target declares, as the destinations of the composition root answer."""

    def __init__(self, env: ExportEnv, default: TargetCapabilities) -> None:
        self.default = default
        self.by_target = {env.target_id: default}

    def __call__(self, target_id: str) -> TargetCapabilities | None:
        return self.by_target.get(target_id, self.default)


@dataclass
class RegistryPlans(Plans):
    """The plans of the destinations, each target with the capabilities it declares."""

    registry: Registry | None = None

    def capabilities(self, target_id: str) -> TargetCapabilities:
        assert self.registry is not None
        found = self.registry(target_id)
        assert found is not None
        return found


def ref(item: str) -> ContentRef:
    return ContentRef(kind="export_item", ref=item)


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# -- the source of the bytes (EF-BIB-045) ---------------------------------------------------------


@pytest.mark.req("EF-BIB-045", "EF-EXP-058")
def test_fan_out_reads_bytes_from_store_or_verified_destination() -> None:
    with export_env() as env:
        series = env.new_series()
        unit = unit_with_item(env, series)
        registry = Registry(env, caps("fetch"))
        host = HostContentStore(env.engine, capabilities=registry)

        # the library copy, then /store: the bytes are the canonical ones, whichever copy serves
        assert host.read(env.destination_id, ref(unit.item_id)) == unit.data
        stored = put_store_copy(env, unit)
        drop_library_copy(env, unit)
        assert host.read(env.destination_id, ref(unit.item_id)) == unit.data

        # no /store, no library: a file of a destination whose hash is the manifest's, readable back
        stored.unlink()
        _forget_store(env)
        written = put_destination_copy(env, env.destination_id, unit, unit.data)
        assert host.read(env.destination_id, ref(unit.item_id)) == unit.data

        # the server cannot read a copy back (`readback` none): the file is not a source
        registry.by_target[env.target_id] = caps("none")
        with pytest.raises(ContentNeedsDownloadError):
            host.read(env.destination_id, ref(unit.item_id))

        # the file changed since the manifest: it is not the bytes the manifest names
        registry.by_target[env.target_id] = caps("verify")
        written.write_bytes(unit.data + b"tampered")
        with pytest.raises(ContentNeedsDownloadError):
            host.read(env.destination_id, ref(unit.item_id))


@pytest.mark.req("EF-BIB-045")
def test_nothing_to_read_asks_for_a_new_download_and_serves_nothing() -> None:
    with export_env() as env:
        series = env.new_series()
        unit = unit_with_item(env, series)
        drop_library_copy(env, unit)
        asked: list[str] = []
        host = HostContentStore(
            env.engine, capabilities=Registry(env, caps("fetch")), download=asked.append
        )

        with pytest.raises(ContentNeedsDownloadError) as caught:
            host.read(env.destination_id, ref(unit.item_id))

        assert caught.value.code == "export.source_missing"
        assert asked == [unit.unit_id]  # one download asked for, for this Unit and no other


@pytest.mark.req("EF-BIB-045")
def test_a_destination_copy_of_another_shelf_is_never_a_source() -> None:
    """The cloister of a family shelf: an Edition moved to a family shelf leaves its adult copy in
    the adult tree; the family binding does not read it (a copy of its own shelf only)."""
    with export_env() as env:
        series = env.new_series()
        unit = unit_with_item(env, series)
        adult_shelf, adult_destination = _adult_binding(env)
        put_destination_copy(env, adult_destination, unit, unit.data, folder="Adult")
        drop_library_copy(env, unit)
        host = HostContentStore(env.engine, capabilities=Registry(env, caps("fetch")))

        with pytest.raises(ContentNeedsDownloadError):
            host.read(env.destination_id, ref(unit.item_id))
        assert adult_shelf != env.shelf_id


# -- the rendering is made again (EF-BIB-048) -----------------------------------------------------


@pytest.mark.req("EF-BIB-048", "EF-BIB-045")
def test_fan_out_never_copies_other_destination_rendering() -> None:
    with export_env() as env:
        series = env.new_series()
        unit = unit_with_item(env, series)
        registry = Registry(env, caps("fetch"))
        host = HostContentStore(env.engine, capabilities=registry)
        second = bind(env, "Second", target_id=other_target(env, caps("fetch"), registry.by_target))
        rendering = unit.data + b"<ComicInfo>Series of the first destination</ComicInfo>"
        put_destination_copy(env, env.destination_id, unit, rendering)
        drop_library_copy(env, unit)

        # the file of the first binding is a rendering (its hash is not the archive's): the second
        # binding does not copy it, and neither does the first read its own back as a source
        for destination in (second, env.destination_id):
            with pytest.raises(ContentNeedsDownloadError):
                host.read(destination, ref(unit.item_id))


@pytest.mark.req("EF-BIB-048", "EF-BIB-045")
def test_each_destination_has_a_file_of_its_own_made_from_the_canonical_archive() -> None:
    with export_env() as env:
        registry = Registry(env, caps("fetch"))
        fan, plans, _probe = _host_pass(env, registry)
        series = env.new_series()
        unit = unit_with_item(env, series)
        second = bind(env, "Second", target_id=other_target(env, caps("fetch"), registry.by_target))
        plans.by_destination = {
            env.destination_id: write_plan(unit.item_id, folder="One"),
            second: write_plan(unit.item_id, folder="Two"),
        }

        fan.run(env.shelf_id, "call-1")

        one = env.lib.manga / "Kavita" / "One" / "Volume 1.cbz"
        two = env.lib.manga / "Second" / "Two" / "Volume 1.cbz"
        assert one.read_bytes() == unit.data == two.read_bytes()
        (d1, i1, n1), (d2, i2, n2) = inode(one), inode(two)
        assert (d1, i1) != (d2, i2) and n1 == n2 == 1  # two files, never one inode under two names
        one.write_bytes(b"altered by the first server")
        assert two.read_bytes() == unit.data  # the other copy is not the one that changed
        assert Path(str(unit.source.local_path)).read_bytes() == unit.data


@pytest.mark.req("EF-BIB-048")
def test_a_rendering_that_fails_on_one_binding_marks_nothing_on_the_others() -> None:
    with export_env() as env:
        registry = Registry(env, caps("fetch"))
        fan, plans, _probe = _host_pass(env, registry)
        second = bind(
            env, "Second",
            target_id=other_target(env, caps("fetch", ("pdf",)), registry.by_target),
        )  # fmt: skip
        series = env.new_series()
        unit = unit_with_item(env, series)
        first = env.destination_id
        plans.by_destination = {
            first: write_plan(unit.item_id, folder="One"),
            second: write_plan(unit.item_id, folder="Two"),
        }

        report = fan.run(env.shelf_id, "call-1")

        status = {o.destination_id: (o.status, o.code) for o in report.outcomes}
        assert status[first] == ("done", None)
        assert status[second][0] == "failed"  # no native format for the second server
        assert (env.lib.manga / "Kavita" / "One" / "Volume 1.cbz").read_bytes() == unit.data
        with env.engine.connect() as connection:
            lines = (
                connection.execute(sa.text("SELECT shelf_destination_id FROM export_record"))
                .scalars()
                .all()
            )
        assert second not in lines


# -- the native format (EF-EXP-046) ---------------------------------------------------------------


def _offers(*formats: str) -> list[ContentOffer]:
    return [
        ContentOffer(f"c-{f}", f, "application/pdf" if f == "pdf" else f"application/{f}")
        for f in formats
    ]


@pytest.mark.req("EF-EXP-046")
def test_accepted_content_types_selects_native_format() -> None:
    offers = _offers("cbz", "pdf")
    # the order of the destination decides, not the order of the Variant
    assert choose_content(["pdf", "cbz"], offers) == offers[1]
    assert choose_content(["cbz", "pdf"], offers) == offers[0]
    # the first one the Variant has
    assert choose_content(["epub", "cbz"], offers) == offers[0]
    # a MIME type names a content too
    assert choose_content(["application/pdf"], offers) == offers[1]
    # none native: no format (the Rendition of M5 is reserved, never invented here)
    assert choose_content(["epub"], offers) is None
    assert choose_content([], offers) is None
    assert choose_content(["cbz"], []) is None
    assert NO_NATIVE_FORMAT == "export.no_native_format"
    # deterministic: the same answer however the offers are listed
    assert choose_content(["cbz"], [*offers, offers[0]]) == choose_content(["cbz"], offers)


@pytest.mark.req("EF-EXP-046", "EF-EXP-058")
def test_each_binding_gets_the_native_format_of_its_own_server() -> None:
    with export_env() as env:
        series = env.new_series()
        unit = unit_with_item(env, series)
        pdf = b"%PDF-1.7 a pdf of the chapter"
        add_format(env, unit, "pdf", pdf)
        registry = Registry(env, caps("none", ("pdf", "cbz")))
        host = HostContentStore(env.engine, capabilities=registry)
        reads_cbz = bind(
            env, "Second", target_id=other_target(env, caps("none", ("cbz",)), registry.by_target)
        )
        neither = bind(
            env, "Third", target_id=other_target(env, caps("none", ("epub",)), registry.by_target)
        )

        assert host.read(env.destination_id, ref(unit.item_id)) == pdf
        assert host.read(reads_cbz, ref(unit.item_id)) == unit.data
        with pytest.raises(PlanRefusedError) as caught:
            host.read(neither, ref(unit.item_id))
        assert NO_NATIVE_FORMAT in str(caught.value.code) or caught.value.reason == NO_NATIVE_FORMAT


@pytest.mark.req("EF-EXP-046", "EF-EXP-058")
def test_a_native_format_never_gets_around_the_external_egress_guard() -> None:
    """The watermark is the one of the content that leaves: a format picked for a binding whose
    egress is external is refused when that content is watermarked, as the planned item is."""
    with export_env() as env:
        series = env.new_series()
        unit = unit_with_item(env, series)
        content_id = add_format(env, unit, "pdf", b"%PDF-1.7 marked")
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.text("UPDATE content SET watermarked = 1 WHERE id = :c"), {"c": content_id}
            )
            connection.execute(
                sa.text("UPDATE shelf_destination SET egress = 'externe' WHERE id = :d"),
                {"d": env.destination_id},
            )
        host = HostContentStore(
            env.engine, capabilities=Registry(env, caps("none", ("pdf", "cbz")))
        )

        with pytest.raises(PlanRefusedError):
            host.read(env.destination_id, ref(unit.item_id))


@pytest.mark.req("EF-EXP-046", "EF-EXP-002")
def test_the_host_serves_an_item_of_another_shelf_to_no_binding_whatever_the_format() -> None:
    with export_env() as env:
        series = env.new_series()
        unit = unit_with_item(env, series)
        other_shelf, other_destination = _adult_binding(env)
        host = HostContentStore(env.engine, capabilities=Registry(env, caps("fetch")))

        with pytest.raises(PlanRefusedError, match="no such content to this binding"):
            host.read(other_destination, ref(unit.item_id))
        assert other_shelf != env.shelf_id


@pytest.mark.req("EF-BIB-045", "EF-EXP-047")
def test_a_family_binding_is_served_no_bytes_above_its_ceiling_or_unrated() -> None:
    """The cloister of a family shelf, on the road of the bytes: what is rated within the ceiling
    is served, what is above it or unrated is not, and a ceiling lowered afterwards closes it."""
    from tests.family_env import family_env, rate

    with family_env(13) as env:
        registry = Registry(env, caps("fetch"))
        host = HostContentStore(env.engine, capabilities=registry)
        fine, old, unrated = env.new_series("Fine"), env.new_series("Old"), env.new_series("New")
        rate(env, fine, 10)
        rate(env, old, 18)
        served = unit_with_item(env, fine)
        too_old = unit_with_item(env, old)
        waiting = unit_with_item(env, unrated)

        assert host.read(env.destination_id, ref(served.item_id)) == served.data
        for refused in (too_old, waiting):
            with pytest.raises(PlanRefusedError, match="no such content to this binding"):
                host.read(env.destination_id, ref(refused.item_id))
        with write_transaction(env.engine) as connection:
            connection.execute(sa.text("UPDATE shelf SET max_age_rating = 6"))
        with pytest.raises(PlanRefusedError, match="no such content to this binding"):
            host.read(env.destination_id, ref(served.item_id))


@pytest.mark.req("EF-EXP-046", "EF-EXP-058")
def test_the_planned_refs_of_a_binding_are_the_items_it_has_a_native_format_for() -> None:
    with export_env() as env:
        series = env.new_series()
        unit = unit_with_item(env, series)
        with env.engine.connect() as connection:
            both = planned_content(connection, env.destination_id, ["pdf", "cbz"])
            none = planned_content(connection, env.destination_id, ["epub"])
            unfiltered = planned_content(connection, env.destination_id)
            formats = native_formats(connection, [unit.item_id], ["pdf", "cbz"])
        assert both.planned_refs == unfiltered.planned_refs == {unit.item_id}
        assert none.planned_refs == frozenset()  # nothing here is in a format that server takes
        assert formats == {unit.item_id: "cbz"}


@pytest.mark.req("EF-EXP-046", "EF-EXP-001")
def test_a_plan_names_the_file_with_the_format_chosen_for_its_binding() -> None:
    """A destination that names the file of an item by another format than the one the host serves
    it in is refused: the manifest never says "cbz" of the bytes of a pdf."""
    with export_env() as env:
        registry = Registry(env, caps("fetch", ("pdf",)))
        fan, plans, _probe = _host_pass(env, registry)
        series = env.new_series()
        unit = unit_with_item(env, series)
        add_format(env, unit, "pdf", b"%PDF-1.7 chapter")
        plans.by_destination = {env.destination_id: write_plan(unit.item_id, folder="One")}

        refused = fan.run(env.shelf_id, "call-1").outcomes[0]

        assert refused.status == "failed"  # "Volume 1.cbz" for the bytes of a pdf
        assert not (env.lib.manga / "Kavita" / "One" / "Volume 1.cbz").exists()


@pytest.mark.req("EF-BIB-045", "EF-BIB-048")
def test_the_registry_keeps_the_archives_a_plan_wrote_and_never_their_renderings() -> None:
    with export_env() as env:
        fan, plans, _probe = _host_pass(env, Registry(env, caps("fetch")))
        series = env.new_series()
        unit = unit_with_item(env, series)
        second = bind(env, "Second")
        plans.by_destination = {
            env.destination_id: write_plan(unit.item_id, folder="One"),
            second: write_plan(unit.item_id, folder="Two"),
        }

        fan.run(env.shelf_id, "call-1")
        fan.run(env.shelf_id, "call-1")  # the pass resumed: it adds no row

        with env.engine.connect() as connection:
            held = connection.execute(
                sa.text(
                    "SELECT rel_path, file_hash, independent, mutable_by_third_party "
                    "FROM replica WHERE location = 'destination' ORDER BY rel_path"
                )
            ).all()
            copies = independent_copies(connection, unit.unit_id)
        # one row per tree, neither forgot the other, none counted as a copy of its own
        assert [r.rel_path for r in held] == [
            "manga/Kavita/One/Volume 1.cbz",
            "manga/Second/Two/Volume 1.cbz",
        ]
        assert {(r.file_hash, r.independent, r.mutable_by_third_party) for r in held} == {
            (digest(unit.data), 0, 1)
        }
        assert copies == 1


@pytest.mark.req("EF-BIB-045")
def test_a_file_of_another_size_than_the_registry_holds_is_no_source() -> None:
    with export_env() as env:
        series = env.new_series()
        unit = unit_with_item(env, series)
        host = HostContentStore(env.engine, capabilities=Registry(env, caps("fetch")))
        written = put_destination_copy(env, env.destination_id, unit, unit.data)
        drop_library_copy(env, unit)
        written.write_bytes(unit.data + b"x" * 10)  # grew since the registry looked
        with pytest.raises(ContentNeedsDownloadError):
            host.read(env.destination_id, ref(unit.item_id))


@pytest.mark.req("EF-BIB-051", "EF-EXP-046")
def test_the_epub_is_served_only_at_the_hash_that_was_verified(tmp_path: Path) -> None:
    with export_env() as env:
        series = env.new_series()
        unit = unit_with_item(env, series)
        epub_id = add_format(env, unit, "epub", b"PK epub derivative")
        path = tmp_path / "c.epub"
        path.write_bytes(b"PK clean derivative")
        good = EpubFile(str(path), digest(b"PK clean derivative"), epub_id)
        registry = Registry(env, caps("fetch", ("epub",)))
        served = HostContentStore(env.engine, capabilities=registry, epub_path=lambda _u: good)
        assert served.read(env.destination_id, ref(unit.item_id)) == b"PK clean derivative"
        # another `content` than the one chosen (the watermark was read on that one)
        other = EpubFile(str(path), good.file_hash, "01HZZZZZZZZZZZZZZZZZZZZZZZ")
        mismatched = HostContentStore(env.engine, capabilities=registry, epub_path=lambda _u: other)
        with pytest.raises(PlanRefusedError):
            mismatched.read(env.destination_id, ref(unit.item_id))


@pytest.mark.req("EF-BIB-045", "EF-BIB-048")
def test_the_registry_follows_a_rename_and_a_recycle_of_the_plan() -> None:
    with export_env() as env:
        registry = Registry(env, caps("fetch"))
        fan, plans, _probe = _host_pass(env, registry)
        series = env.new_series()
        unit = unit_with_item(env, series)
        plans.by_destination = {env.destination_id: write_plan(unit.item_id, folder="One")}
        fan.run(env.shelf_id, "call-1")

        def rows() -> list[str]:
            with env.engine.connect() as connection:
                return list(
                    connection.execute(
                        sa.text("SELECT rel_path FROM replica WHERE location = 'destination'")
                    ).scalars()
                )

        assert rows() == ["manga/Kavita/One/Volume 1.cbz"]
        plans.by_destination = {
            env.destination_id: {
                "ops": [
                    {
                        "op": "rename",
                        "rel_path": "One/Volume 1.cbz",
                        "dst_rel_path": "One/Vol 1.cbz",
                    }
                ]
            }
        }
        fan.run(env.shelf_id, "call-2")
        assert rows() == ["manga/Kavita/One/Vol 1.cbz"]
        plans.by_destination = {
            env.destination_id: {"ops": [{"op": "recycle", "rel_path": "One/Vol 1.cbz"}]}
        }
        fan.run(env.shelf_id, "call-3")
        assert rows() == []  # the file is in the bin: nothing says it is here


@pytest.mark.req("EF-EXP-046")
def test_an_item_that_lost_its_native_format_is_said_and_its_file_is_left() -> None:
    with export_env() as env:
        registry = Registry(env, caps("fetch"))
        fan, plans, _probe = _host_pass(env, registry)
        series = env.new_series()
        unit = unit_with_item(env, series)
        plans.by_destination = {env.destination_id: write_plan(unit.item_id, folder="One")}
        fan.run(env.shelf_id, "call-1")
        registry.by_target[env.target_id] = caps("fetch", ("pdf",))  # cbz no longer accepted
        plans.by_destination = {}

        outcome = fan.run(env.shelf_id, "call-2").outcomes[0]

        assert outcome.lost_format == (unit.item_id,)
        assert (env.lib.manga / "Kavita" / "One" / "Volume 1.cbz").exists()  # never removed here


# -- helpers ------------------------------------------------------------


def _forget_store(env: ExportEnv) -> None:
    with write_transaction(env.engine) as connection:
        connection.execute(sa.text("DELETE FROM replica WHERE location = 'store'"))


def _adult_binding(env: ExportEnv) -> tuple[str, str]:
    from tests.export_env import add_second_shelf

    return add_second_shelf(env)


def _host_pass(env: ExportEnv, registry: Registry) -> tuple[FanOut, RegistryPlans, object]:
    """A fan-out pass whose bytes are the host's own, over the registry of copies."""
    _fan, _plans, _contents, probe = setup(env)
    env.pipeline.use_plan_executor(
        contents=HostContentStore(env.engine, capabilities=registry), probe=probe
    )
    plans = RegistryPlans(registry=registry)
    return FanOut(env.engine, env.pipeline, plans, clock=env.clock), plans, probe
