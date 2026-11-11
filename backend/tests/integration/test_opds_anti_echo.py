"""M2 L22 (EF-SRC-006, I50, EF-SRC-023): an OPDS feed that leads back to Shelfhold's own files.

A catalogue may serve what Shelfhold exported (Kavita, a replica of it, Shelfhold's own OPDS
catalogue): the file is a replica of the `export_item`, never a Variant. A source whose address is
the instance of an active destination says "use readback" instead of importing its own output."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.domain.naming import DEFAULT_TEMPLATE
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import export
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.plugin_host.native_sources import opds_source
from shelfhold.plugin_host.settings import SourceSettings, parse_settings_schema
from shelfhold.sdk.testing.corpus.trapped_epubs.build import epub_bytes, xhtml
from tests.anti_echo_env import seed_own_export
from tests.opds_env import KEY, opds_bench, serve_komga
from tests.redaction_support import forget_secret_values

OWN = epub_bytes(xhtml("<p>ce que Shelfhold a exporté</p>"))


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    forget_secret_values()
    yield
    forget_secret_values()


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def acquire(b: Any) -> Any:
    async def go() -> Any:
        ref = (await b.acquirer.search(KEY, "titre")).items[0]
        prepared = await b.acquirer.prepare(ref)
        assert prepared.request_id is not None
        return await b.acquirer.fetch(prepared.request_id, b.store(), b.place)

    return asyncio.run(go())


def counts(b: Any) -> dict[str, int]:
    return {
        t: b.rows(f"SELECT count(*) AS n FROM {t}")[0]["n"]  # noqa: S608
        for t in ("unit_variant", "content")
    }


# -- the rule, through a feed ------------------------------------------------------------------


@pytest.mark.req("EF-SRC-006")
def test_opds_source_reimport_of_own_export_is_replica_not_variant() -> None:
    """A Komga-shaped catalogue serves the file Shelfhold exported: a replica, no Variant."""
    with opds_bench(auth="basic", base_path="/opds") as b:
        own = seed_own_export(b.lib, file_hash=sha(OWN))
        serve_komga(b, {"tome-1.epub": OWN})
        outcome = acquire(b)
        assert outcome.state == "replica" and outcome.reason == "own_export"
        assert counts(b) == {"unit_variant": 0, "content": 0}
        (replica,) = b.rows("SELECT export_item_id, location FROM replica")
        assert replica["export_item_id"] == own.export_item_id
        assert replica["location"] == "upstream"
        assert not b.lib.files_under(b.lib.manga) and not b.lib.files_under(b.lib.store)


@pytest.mark.req("EF-SRC-006")
def test_a_feed_that_points_at_a_replica_or_at_shelfhold_itself_makes_no_variant() -> None:
    """Attack: the same own file reached through three catalogues (the Kavita we write to, a
    replica of it, Shelfhold's own OPDS catalogue), each with its own entry, own id and own link:
    every one is a replica of the one export, and none is a Variant."""
    from tests.opds_env import serve_kavita

    for label in ("kavita", "replica", "shelfhold"):
        with opds_bench(auth="api_key_path", base_path="/api/opds/{apiKey}") as b:
            own = seed_own_export(b.lib, file_hash=sha(OWN))
            serve_kavita(b, {f"{label}.epub": OWN})
            outcome = acquire(b)
            assert outcome.state == "replica", label
            assert counts(b)["unit_variant"] == 0, label
            (replica,) = b.rows("SELECT export_item_id FROM replica")
            assert replica["export_item_id"] == own.export_item_id


@pytest.mark.req("EF-SRC-006")
def test_opds_rewritten_sidecar_is_still_an_echo() -> None:
    """The server rewrote what is around the payload: the payload hash still says it is ours."""
    from tests.integration.test_anti_echo import epub_payload

    served = OWN
    with opds_bench(auth="basic", base_path="/opds") as b:
        seed_own_export(
            b.lib, file_hash=sha(b"another rendering"), content_hash=epub_payload(served)
        )
        serve_komga(b, {"tome-1.epub": served})
        assert acquire(b).state == "replica"
        assert counts(b)["unit_variant"] == 0


@pytest.mark.req("EF-SRC-006")
def test_opds_book_that_is_not_ours_is_a_variant_as_always() -> None:
    with opds_bench(auth="basic", base_path="/opds") as b:
        seed_own_export(b.lib, file_hash=sha(OWN))
        serve_komga(b, {"tome-1.epub": epub_bytes(xhtml("<p>un livre d'ailleurs</p>"))})
        assert acquire(b).state == "fetched"
        assert counts(b)["unit_variant"] == 1


# -- "use readback" ---------------------------------------------------------------------------


@pytest.fixture
def store(settings: Settings) -> Iterator[tuple[sa.Engine, SourceSettings]]:
    migrate(settings)
    vault = SecretVault(boot_check(settings, announce=False))
    engine = create_db_engine(settings.database_path)
    yield engine, SourceSettings(engine, vault)
    engine.dispose()


def add_destination(engine: sa.Engine, url: str, *, enabled: bool = True) -> None:
    target, shelf, destination = new_ulid(), new_ulid(), new_ulid()
    with engine.begin() as connection:
        connection.execute(
            sa.insert(export.export_target).values(
                id=target, kind="kavita", name=f"Kavita {target[-4:]}", path_map=[],
                config={"url": url},
            )
        )  # fmt: skip
        connection.execute(sa.insert(export.shelf).values(id=shelf, name="Livres", phrase="x"))
        connection.execute(
            sa.insert(export.shelf_destination).values(
                id=destination, shelf_id=shelf, target_id=target, transport="tree",
                egress="foyer", root_subpath="books", template=DEFAULT_TEMPLATE.dumps(),
                template_origin=f"default@{DEFAULT_TEMPLATE.version}", enabled=enabled,
            )
        )  # fmt: skip


def configure(store: SourceSettings, base_url: str) -> Any:
    schema = parse_settings_schema(opds_source().manifest.settings_schema)
    from shelfhold.headless.actor import Actor

    store.set_values(KEY, schema, {"base_url": base_url}, actor=Actor("session:admin-test"))
    return store.view(KEY, schema)


@pytest.mark.req("EF-SRC-006")
def test_opds_source_on_active_destination_warns_use_readback(
    store: tuple[sa.Engine, SourceSettings],
) -> None:
    engine, settings = store
    add_destination(engine, "http://kavita.lan:5000")
    for url in (
        "http://kavita.lan:5000/api/opds/{apiKey}",
        "http://KAVITA.lan:5000/",
        "http://kavita.lan:5000/api/opds/abc/series",
    ):
        assert configure(settings, url).warnings == ("source.use_readback",), url
    # another instance, another port, another scheme: no warning
    for url in (
        "http://kavita.lan:5001/api/opds",
        "http://other.lan:5000/api/opds",
        "https://kavita.lan:5000/api/opds",
    ):
        assert configure(settings, url).warnings == (), url


@pytest.mark.req("EF-SRC-006")
def test_opds_source_on_a_disabled_or_absent_destination_does_not_warn(
    store: tuple[sa.Engine, SourceSettings],
) -> None:
    engine, settings = store
    assert configure(settings, "http://kavita.lan:5000/api/opds").warnings == ()
    add_destination(engine, "http://kavita.lan:5000", enabled=False)
    assert configure(settings, "http://kavita.lan:5000/api/opds").warnings == ()


@pytest.mark.req("EF-SRC-006")
def test_use_readback_warning_never_shows_a_credential(
    store: tuple[sa.Engine, SourceSettings],
) -> None:
    """The warning is a closed code: neither the address nor what is in it is repeated."""
    engine, settings = store
    add_destination(engine, "http://kavita.lan:5000")
    view = configure(settings, "http://kavita.lan:5000/api/opds/{apiKey}")
    assert view.warnings == ("source.use_readback",)
    assert "kavita" not in repr(view.warnings)
