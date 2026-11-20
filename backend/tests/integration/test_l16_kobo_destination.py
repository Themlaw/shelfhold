"""EF-EXP-021 (M6 L16, D19): the Kobo folder destination — a tree of `.kepub.epub`.

The folder is the one the household's sync tool, or the mounted reader, reads (transport `tree`).
Every file carries the extension a Kobo wants (`.kepub.epub`, mandatory) and the bytes of the KEPUB
of the **neutralized derivative** (I35): the Python conversion of M5 through the rendition cache,
never the received original. A folder keeps no account apart (`access_partitioning = none`): a
shelf below 18+ is refused, as it is for every destination that partitions nothing.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from shelfhold.export.application.fan_out import EpubFile, FanOut
from shelfhold.export.domain.bindings import (
    BindingError,
    TargetKindTransportError,
    UnpartitionedDestinationError,
)
from shelfhold.infrastructure.schema import storage
from shelfhold.library.application.kepub import kepubify
from tests.export_env import ExportEnv, export_env
from tests.fan_out_env import service
from tests.kobo_env import (
    CHAPTER,
    add_book,
    bind_kobo,
    kobo_target,
    lower_ceiling,
    member,
    records,
    renditions,
    rig,
    tree_files,
)


def _run(fan: FanOut, env: ExportEnv, call: str, destination: str) -> tuple[str, int]:
    outcome = next(
        o for o in fan.run(env.shelf_id, call).outcomes if o.destination_id == destination
    )
    return outcome.status, outcome.executed


@pytest.mark.req("EF-EXP-021")
def test_kobo_destination_kepubify() -> None:
    """One book on the shelf: the folder receives one `.kepub.epub`, the KEPUB of the derivative
    (the spans of the conversion of M5), made through the rendition cache — a second pass plans
    nothing, and the cache holds the one rendition."""
    with export_env() as env:
        store: dict[str, EpubFile] = {}
        book = add_book(env, env.new_series("Synthetic Book"), "1", store)
        destination = bind_kobo(env)
        rends = renditions(env)
        fan, _source = rig(env, store, rends)
        assert _run(fan, env, "c1", destination) == ("done", 2)  # the folder and the book
        files = tree_files(env)
        assert len(files) == 1
        assert files[0].name.endswith(".kepub.epub")  # the extension a Kobo wants, mandatory
        data = files[0].read_bytes()
        chapter = member(data, CHAPTER)
        assert b"koboSpan" in chapter  # the spans of the conversion, not the EPUB it came from
        assert data == kepubify(book.data)  # the KEPUB of the derivative, byte for byte
        with env.engine.connect() as connection:
            kinds = (
                connection.execute(
                    sa.select(storage.rendition.c.kind).order_by(storage.rendition.c.kind)
                )
                .scalars()
                .all()
            )
        assert kinds == ["kepub"]  # the cache of M5 L20b made it, not the pass
        assert _run(fan, env, "c2", destination) == ("done", 0)  # written once, never rewritten
        assert len(records(env, destination)) == 1


@pytest.mark.req("EF-EXP-021")
def test_kobo_destination_never_serves_untrusted_original() -> None:
    """The received original is in the library, as the library holds it; the KEPUB is made from
    the verified derivative alone. A file that carries a word of the original never reaches the
    reader."""
    with export_env() as env:
        store: dict[str, EpubFile] = {}
        book = add_book(env, env.new_series("Synthetic Book"), "1", store)
        assert b"original" in member(book.original, CHAPTER)
        assert b"original" not in member(book.data, CHAPTER)
        destination = bind_kobo(env)
        fan, _source = rig(env, store, renditions(env))
        assert _run(fan, env, "c1", destination) == ("done", 2)  # the folder and the book
        data = tree_files(env)[0].read_bytes()
        assert b"original" not in member(data, CHAPTER)  # nothing of the received file
        assert data == kepubify(book.data)  # the neutralized derivative, converted


@pytest.mark.req("EF-EXP-021", "EF-EXP-047")
@pytest.mark.parametrize("ceiling", [13, 10, 6])
def test_kobo_destination_refuses_family_shelf_without_partitioning(ceiling: int) -> None:
    """A shelf below 18+ is never linked to a Kobo folder, which keeps no account apart: refused
    when it is bound, whatever the target claims about itself."""
    with export_env(max_age_rating=ceiling) as env:
        for partitioning in ("none", "library", "user"):
            with pytest.raises(UnpartitionedDestinationError) as refused:
                service(env).bind_destination(
                    shelf_id=env.shelf_id,
                    target_id=kobo_target(env, partitioning=partitioning),
                    transport="tree",
                    plugin_egress="foyer",
                    root_subpath="Kobo",
                )
            assert refused.value.code == "shelf.destination_not_partitioned"
        with env.engine.connect() as connection:
            bound = connection.exec_driver_sql(
                "SELECT count(*) FROM shelf_destination d JOIN export_target t "
                "ON t.id = d.target_id WHERE t.kind = 'kobo'"
            ).scalar_one()
        assert bound == 0


@pytest.mark.req("EF-EXP-021", "EF-EXP-047", "EF-EXP-065")
def test_the_ceiling_lowered_after_the_binding_stops_the_export() -> None:
    """The binding was made on an adult shelf; the ceiling lowered after it makes the pass refuse
    (the rule is looked at again at every pass), and nothing is written."""
    with export_env(max_age_rating=18) as env:
        store: dict[str, EpubFile] = {}
        add_book(env, env.new_series("Synthetic Book"), "1", store)
        destination = bind_kobo(env)
        fan, _source = rig(env, store, renditions(env))
        lower_ceiling(env, 13)  # the shelf became a family shelf
        outcome = next(
            o for o in fan.run(env.shelf_id, "c1").outcomes if o.destination_id == destination
        )
        assert outcome.status == "refused"
        assert outcome.code == "shelf.destination_not_partitioned"
        assert tree_files(env) == [] and records(env, destination) == []


@pytest.mark.req("EF-EXP-021")
@pytest.mark.parametrize("transport", ["dropbox", "pull", "push", "carrier"])
def test_a_kobo_target_is_bound_only_as_a_tree(transport: str) -> None:
    """A Kobo folder is a folder the reader mounts: it is bound as a tree, never as anything
    else (the `pull` surface of the sync comes with its own lot, on the same declaration)."""
    with export_env() as env:
        with pytest.raises(BindingError) as refused:
            service(env).bind_destination(
                shelf_id=env.shelf_id,
                target_id=kobo_target(env),
                transport=transport,
                plugin_egress="foyer",
                root_subpath="Kobo",
            )
        assert isinstance(refused.value, TargetKindTransportError)
        assert refused.value.code == "target.transport_not_supported"
