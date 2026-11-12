"""M2 L31: the debts of the milestone, each with a test that tries to break it.

An arrival from a source goes through the Work tombstones; a file dropped in the import folder that
is an export of Shelfhold's own is a replica; the offline adoption of a restore refuses a known root
that is not mounted; a metadata plugin written for a later SDK is refused.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import AcquireRefused
from shelfhold.export.application.restore import (
    AdoptionBlockedError,
    DeletionRefusedError,
    ForgetRootRefusedError,
    forget_root_phrase,
)
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import platform
from shelfhold.library.application.anti_echo import received_content_hash
from shelfhold.plugin_host import provider_fetcher
from shelfhold.plugin_host.adapters.native.openlibrary import OpenLibraryProvider
from shelfhold.sdk.metadata import EntityRef, MetadataManifest
from shelfhold.sdk.refs import SeriesInfo, SeriesRef
from tests.acquire_env import NativeBooks, acquire_bench
from tests.anti_echo_env import seed_own_export
from tests.import_env import CLEAN_EPUB, import_env
from tests.null_tombstones import NoCatalogHits
from tests.restore_env import restore_env

GUTENBERG_ID = ("gutenberg", "42")


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


# -- the arrival of a source goes through the Work tombstones (debt 2) ----------------------------


class WithIds(NativeBooks):
    """A source that gives the identifiers of its Work and the death year of its author."""

    async def get_series(self, f: Any, ref: SeriesRef) -> SeriesInfo:
        return SeriesInfo(
            ref=ref, title="Un livre de test", authors=["Personne"],
            author_death_years={"Personne": 1900},
            external_ids={GUTENBERG_ID[0]: GUTENBERG_ID[1]},
        )  # fmt: skip


class Purged(NoCatalogHits):
    """A screen where the Work identifiers it was given are purged."""

    def __init__(self, held: set[tuple[str, str]]) -> None:
        self.held = held

    def work_ids(self, ids: Any) -> set[tuple[str, str]]:
        return {(i.provider, i.value) for i in ids if (i.provider, i.value) in self.held}


def nothing_made(b: Any) -> bool:
    return all(
        b.rows(f"SELECT count(*) AS n FROM {table}")[0]["n"] == 0  # noqa: S608
        for table in ("work", "edition", "external_id", "source_item", "source_binding")
    )


@pytest.mark.req("EF-BIB-053", "EF-BIB-003", "EF-SRC-015")
def test_a_purged_work_does_not_come_back_by_an_acquisition() -> None:
    with acquire_bench(WithIds(), work_screen=lambda: Purged({GUTENBERG_ID})) as b:
        with pytest.raises(AcquireRefused) as refused:
            run(b.acquirer.prepare(b.plugin._series()))
        assert refused.value.code == "acquire.work_tombstoned"
        assert nothing_made(b)  # no Work, no Edition, no identifier, no request


@pytest.mark.req("EF-BIB-053", "EF-BIB-003")
def test_an_acquisition_with_identifiers_and_no_screen_fails_closed() -> None:
    with acquire_bench(WithIds(), work_screen=None) as b:  # nothing says whether it was purged
        with pytest.raises(AcquireRefused) as refused:
            run(b.acquirer.prepare(b.plugin._series()))
        assert refused.value.code == "acquire.tombstones_unavailable"
        assert nothing_made(b)


@pytest.mark.req("EF-BIB-003", "EF-SRC-015")
def test_an_acquisition_records_its_identifiers_through_the_arrival() -> None:
    with acquire_bench(WithIds(), work_screen=lambda: NoCatalogHits()) as b:
        prepared = run(b.acquirer.prepare(b.plugin._series()))
        assert prepared.request_id is not None
        [row] = b.rows("SELECT provider, value, origin FROM external_id WHERE entity_type = 'work'")
        assert (row["provider"], row["value"]) == GUTENBERG_ID and row["origin"] == "source"


@pytest.mark.req("EF-SRC-024", "EF-BIB-046")
def test_the_death_year_cache_of_an_arriving_edition_is_derived_from_its_credits() -> None:
    with acquire_bench(WithIds(), work_screen=lambda: NoCatalogHits()) as b:
        run(b.acquirer.prepare(b.plugin._series()))
        [edition] = b.rows("SELECT author_death_year FROM edition")
        assert edition["author_death_year"] == 1900


# -- the import folder obeys the anti-echo rule (debt 1) ------------------------------------------


@pytest.mark.req("EF-SRC-006", "EF-SRC-012")
def test_import_folder_own_export_is_a_replica_and_never_a_unit() -> None:
    with import_env() as env:
        own = seed_own_export(env.lib, file_hash=sha(CLEAN_EPUB))
        env.drop("mon-export.epub", CLEAN_EPUB)
        _scan, outcomes = env.folder.run()
        [outcome] = outcomes
        assert (outcome.state, outcome.code, outcome.removed) == (
            "refused", "import.own_export", False,
        )  # fmt: skip
        assert env.contents() == [] and env.items == []  # no Unit, no Variant, no copy
        assert (env.incoming / "mon-export.epub").read_bytes() == CLEAN_EPUB  # left where it is
        with env.lib.engine.connect() as connection:
            replicas = connection.execute(
                sa.text("SELECT export_item_id, location, independent FROM replica")
            ).all()
        assert [(r.export_item_id, r.location, r.independent) for r in replicas] == [
            (own.export_item_id, "upstream", 0)
        ]
        env.folder.run()  # a second look records nothing twice
        with env.lib.engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM replica")).scalar() == 1


@pytest.mark.req("EF-SRC-006", "EF-SRC-012")
def test_import_folder_echo_is_recognised_by_its_payload_not_only_its_bytes(
    tmp_path: Path,
) -> None:
    """The same book re-zipped (other bytes, same payload) is still the export coming back."""
    aside = tmp_path / "aside.epub"
    aside.write_bytes(CLEAN_EPUB)
    payload = received_content_hash(aside, ".epub")
    assert payload is not None
    with import_env() as env:
        own = seed_own_export(env.lib, content_hash=payload)
        env.drop("autre-nom.epub", CLEAN_EPUB)
        _scan, outcomes = env.folder.run()
        [outcome] = outcomes
        assert outcome.code == "import.own_export"
        assert env.contents() == []
        with env.lib.engine.connect() as connection:
            row = connection.execute(sa.text("SELECT export_item_id FROM replica")).one()
        assert row.export_item_id == own.export_item_id


@pytest.mark.req("EF-SRC-006")
def test_import_folder_another_copy_with_the_same_data_is_not_called_an_echo() -> None:
    """Same payload as the content an export was made from, other bytes: a duplicate copy of the
    book, not our own file coming back (the refusal would be misleading)."""
    with import_env() as env:
        payload = received_content_hash(env.drop("a.epub", CLEAN_EPUB), ".epub")
        seed_own_export(env.lib, kept_content_hash=payload)
        _scan, outcomes = env.folder.run()
        assert [o.code for o in outcomes] != ["import.own_export"]


@pytest.mark.req("EF-SRC-012")
def test_import_folder_file_that_is_not_an_export_is_still_imported() -> None:
    with import_env() as env:
        seed_own_export(env.lib, file_hash=sha(b"another file"))
        env.drop("a.epub", CLEAN_EPUB)
        _scan, outcomes = env.folder.run()
        assert [o.state for o in outcomes] == ["stored"]


# -- the offline adoption refuses a root that is not mounted (debt 6) ----------------------------


@pytest.mark.req("EF-INT-026")
def test_the_adoption_refuses_while_a_known_root_is_not_mounted() -> None:
    with restore_env() as env:
        env.add_chapters(3, 4)
        backup = env.backup()
        env.export()
        env.restore(backup)
        empty_mountpoint = env.library.parent / "not-mounted"
        empty_mountpoint.mkdir()  # the folder is there, the volume is not: no sentinel to read
        with env.engine.begin() as connection:  # a root the database knows
            connection.execute(
                sa.insert(platform.physical_root).values(
                    root_id=new_ulid(), path_entry=str(empty_mountpoint)
                )
            )
        env.open_instance()
        env.guard().enter_restored_behind()
        adopter = env.adopter()
        preview = adopter.preview()
        assert set(preview.blocked_roots.values()) == {"root_not_mounted"}
        with pytest.raises(AdoptionBlockedError) as blocked:
            adopter.adopt(confirm=preview.fingerprint)
        assert blocked.value.reason == "root_not_mounted"
        assert env.guard().assess().behind  # still frozen, nothing was judged


@pytest.mark.req("EF-INT-026")
def test_a_root_that_will_not_come_back_is_forgotten_then_the_adoption_succeeds() -> None:
    with restore_env() as env:
        env.add_chapters(3, 4)
        backup = env.backup()
        env.export()
        env.restore(backup)
        dead = env.library.parent / "dead-disk"
        dead.mkdir()
        dead_id = new_ulid()
        with env.engine.begin() as connection:
            connection.execute(
                sa.insert(platform.physical_root).values(root_id=dead_id, path_entry=str(dead))
            )
        env.open_instance()
        env.guard().enter_restored_behind()
        adopter = env.adopter()
        assert set(adopter.preview().blocked_roots.values()) == {"root_not_mounted"}
        admin = Principal("01J00000000000000000000ADM", "admin", "admin", Credential.LOCAL_CLI)
        phrase = forget_root_phrase(dead_id)
        with pytest.raises(ForgetRootRefusedError) as wrong:
            adopter.forget_root(dead_id, confirm="yes", principal=admin)
        assert wrong.value.reason == "phrase_mismatch"
        with pytest.raises(ForgetRootRefusedError) as unknown:
            adopter.forget_root(new_ulid(), confirm=phrase, principal=admin)
        assert unknown.value.reason == "unknown_root"
        key = Principal("01J00000000000000000000KEY", "api", "admin", Credential.API_KEY)
        with pytest.raises(DeletionRefusedError):  # a key never forgets a root
            adopter.forget_root(dead_id, confirm=phrase, principal=key)
        adopter.forget_root(dead_id, confirm=phrase, principal=admin)
        with env.engine.connect() as connection:
            mode = connection.execute(
                sa.select(platform.physical_root.c.mode).where(
                    platform.physical_root.c.root_id == dead_id
                )
            ).scalar_one()
            audited = connection.execute(
                sa.text("SELECT count(*) FROM audit WHERE action = 'reconcile.forget_root'")
            ).scalar_one()
        assert (mode, audited) == ("adopt_ro", 1)
        preview = adopter.preview()
        assert not preview.blocked_roots
        adopter.adopt(confirm=preview.fingerprint)
        assert not env.guard().assess().behind


@pytest.mark.req("EF-INT-026")
def test_a_root_that_is_mounted_and_readable_cannot_be_forgotten() -> None:
    with restore_env() as env:
        env.add_chapters(3, 4)
        backup = env.backup()
        env.export()
        env.restore(backup)
        env.open_instance()
        env.guard().enter_restored_behind()
        [finding] = [f for f in env.guard().assess().findings][:1]
        admin = Principal("01J00000000000000000000ADM", "admin", "admin", Credential.LOCAL_CLI)
        with pytest.raises(ForgetRootRefusedError) as refused:
            env.adopter().forget_root(
                finding.root_id, confirm=forget_root_phrase(finding.root_id), principal=admin
            )
        assert refused.value.reason == "still_readable"


# -- a metadata plugin written for a later SDK is refused (L11 follow-up) -------------------------


@pytest.mark.req("EF-BIB-013")
def test_a_metadata_provider_for_a_later_sdk_is_refused() -> None:
    manifest = OpenLibraryProvider().manifest
    later = MetadataManifest.model_validate({**manifest.model_dump(mode="json"), "min_sdk": "1.9"})
    with pytest.raises(provider_fetcher.SdkTooOldError):
        provider_fetcher.open_provider(later, None, contact=None)  # type: ignore[arg-type]
    wrong_major = MetadataManifest.model_validate(
        {**manifest.model_dump(mode="json"), "min_sdk": "2.0"}
    )
    with pytest.raises(provider_fetcher.SdkTooOldError):
        provider_fetcher.open_provider(wrong_major, None, contact=None)  # type: ignore[arg-type]


# -- OpenLibrary addresses (url_for, ids_from_url) ------------------------------------------------


@pytest.mark.req("EF-BIB-013")
def test_a_proposal_without_a_work_key_still_cites_the_address_of_the_book() -> None:
    provider = OpenLibraryProvider()
    target = EntityRef(entity_type="edition", ids={"isbn13": "9780306406157"})
    found = provider._proposals({"title": "Un titre"}, target)
    urls = {p.provenance.source_url for p in found.proposals}
    assert urls == {provider.url_for({"isbn13": "9780306406157"})}
