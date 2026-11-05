"""Passthrough storage (EF-BIB-006, EF-BIB-027, EF-BIB-031): the file is kept as received."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.archives import UnsafeArchiveError
from shelfhold.infrastructure.content_trust import UntrustedEntryError
from shelfhold.infrastructure.schema import acquisition, catalog, storage
from shelfhold.library.public import (
    ConfirmationRequiredError,
    DeletionRefusedError,
    PassthroughStore,
    ReceivedTooLargeError,
    confirmation_phrase,
    independent_copies,
    irreplaceability,
    meta_path,
    require_deletion_allowed,
    safe_to_clean,
)
from tests.library_env import OLD_COMICINFO, PNG, LibEnv, cbz_bytes, lib_env

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def admin(credential: Credential = Credential.SESSION, *, age_s: int = 10) -> Principal:
    return Principal(
        "01JC0000000000000000000001", "root", "admin", credential,
        session_id="s1", auth_time=NOW - timedelta(seconds=age_s), key_id="k1",
    )  # fmt: skip


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def store_for(env: LibEnv, **kwargs: int) -> PassthroughStore:
    return PassthroughStore(env.engine, env.fsops, env.locate, env.data, **kwargs)


def ingest(env: LibEnv, data: bytes, *, unit_id: str | None = None, variant_id: str | None = None):
    unit_id = unit_id or env.chain["unit"]
    variant_id = variant_id or env.chain["variant"]
    dest = env.manga / "Dr Stone" / f"Dr Stone - {unit_id[-4:]}.cbz"
    result = store_for(env).ingest(
        unit_id=unit_id, variant_id=variant_id, staged=env.stage(data), dest=str(dest)
    )
    return result, dest


def content_row(env: LibEnv, unit_id: str | None = None) -> sa.Row[tuple[object, ...]]:
    with env.engine.connect() as c:
        return c.execute(
            sa.select(storage.content).where(
                storage.content.c.unit_id == (unit_id or env.chain["unit"])
            )
        ).one()


@pytest.mark.req("EF-BIB-006", "EF-BIB-027")
def test_unit_stored_as_received_with_unit_json() -> None:
    data = cbz_bytes(comicinfo=OLD_COMICINFO)  # whatever the source put in it stays in it
    with lib_env() as env:
        result, dest = ingest(env, data)
        assert dest.read_bytes() == data  # not repackaged, not rendered: as received
        row = content_row(env)
        assert (row.retention, row.path, row.store_provenance) == ("passthrough", None, "received")
        assert (row.media_type, row.format, row.kind) == (
            "application/vnd.comicbook+zip", "cbz", "images",
        )  # fmt: skip
        document = json.loads(meta_path(env.data, env.chain["unit"]).read_text(encoding="utf-8"))
        assert document["schema_version"] == "1.1" and document["unit_id"] == env.chain["unit"]
        assert document["contents"][0]["content_id"] == result.content_id
        assert document["contents"][0]["content_hash"] == row.content_hash


@pytest.mark.req("EF-BIB-006")
def test_received_bytes_sha256_preserved() -> None:
    data = cbz_bytes(comicinfo=OLD_COMICINFO)
    with lib_env() as env:
        result, dest = ingest(env, data)
        row = content_row(env)
        assert row.original_sha256 == result.original_sha256 == sha(data)
        assert sha(dest.read_bytes()) == row.original_sha256 and row.size == len(data)
        with env.engine.connect() as c:
            replica = c.execute(sa.text("SELECT * FROM replica")).mappings().one()
        assert replica["location"] == "library" and replica["file_hash"] == sha(data)
        assert replica["content_hash"] == row.content_hash and replica["independent"]
        assert replica["mutable_by_third_party"]  # Kavita may rewrite it (EF-BIB-045)
        assert (replica["dev"], replica["ino"]) == (dest.stat().st_dev, dest.stat().st_ino)


@pytest.mark.req("EF-BIB-027")
def test_passthrough_mode_leaves_no_file_in_store() -> None:
    with lib_env() as env:
        ingest(env, cbz_bytes())
        assert env.files_under(env.store) == []  # no third copy
        assert len(env.files_under(env.manga)) == 1
        with env.engine.connect() as c:
            assert c.execute(sa.select(storage.content.c.path)).scalars().all() == [None]


@pytest.mark.req("EF-BIB-031")
def test_irreplaceable_never_deleted_without_typed_confirmation() -> None:
    with lib_env() as env:
        unit = env.chain["unit"]
        with env.engine.connect() as c:
            assert not irreplaceability(c, unit).irreplaceable  # a live source
            require_deletion_allowed(c, unit, principal=admin(), now=NOW, typed=None)
        with env.engine.begin() as c:  # the source goes away
            c.execute(
                sa.update(acquisition.source_binding).values(active=False, disabled_reason="manual")
            )
        other, _ = env.new_unit()  # before the read below: a read now holds its snapshot
        with env.engine.connect() as c:
            assert irreplaceability(c, unit).irreplaceable
            for typed in (None, "", "yes", unit.lower()):
                with pytest.raises(ConfirmationRequiredError):
                    require_deletion_allowed(c, unit, principal=admin(), now=NOW, typed=typed)
            granted = require_deletion_allowed(
                c, unit, principal=admin(), now=NOW, typed=confirmation_phrase(unit)
            )
            assert (granted.actor, granted.irreplaceable) == ("session:s1", True)
            # a retention policy or a clean-up never gets to it, with or without words
            allowed, kept = safe_to_clean(c, [unit, other])
            assert (allowed, kept) == ([], [unit, other])  # the other one has no live source either
        with env.engine.begin() as c:
            c.execute(
                sa.update(acquisition.source_binding).values(active=True, disabled_reason=None)
            )
            c.execute(
                sa.update(catalog.unit)
                .where(catalog.unit.c.id == unit)
                .values(availability="removed")
            )
        with env.engine.connect() as c:
            assert irreplaceability(c, unit).irreplaceable  # source "removed": kept and flagged
            assert safe_to_clean(c, [unit, other]) == ([other], [unit])


@pytest.mark.req("EF-BIB-006")
def test_irreplaceable_counts_independent_copies() -> None:
    with lib_env() as env:
        ingest(env, cbz_bytes())
        with env.engine.connect() as c:
            state = irreplaceability(c, env.chain["unit"])
            assert state.independent_copies == independent_copies(c, env.chain["unit"]) == 1


@pytest.mark.req("ENF-SEC-002", "EF-BIB-051")
@pytest.mark.parametrize(
    "bad",
    [
        cbz_bytes((("p.png", PNG), ("page.svg", b"<svg onload=alert(1)/>"))),
        cbz_bytes((("p.png", PNG), ("fake.jpg", b"<html><script>1</script>"))),
        cbz_bytes((("../escape.png", PNG),)),
        b"PK\x03\x04 not a zip at all",
    ],
    ids=["svg_page", "html_as_jpg", "path_escape", "corrupt"],
)
def test_received_cbz_off_the_allowlist_is_refused_and_nothing_is_ingested(bad: bytes) -> None:
    with lib_env() as env:
        with pytest.raises((UntrustedEntryError, UnsafeArchiveError)):
            ingest(env, bad)
        assert env.files_under(env.library) == [] and env.files_under(env.data) == []
        with env.engine.connect() as c:
            assert (
                c.execute(sa.select(sa.func.count()).select_from(storage.content)).scalar_one() == 0
            )


@pytest.mark.req("ENF-SEC-002")
def test_received_cbz_above_the_bound_is_refused_before_it_is_opened() -> None:
    with lib_env() as env:
        data = cbz_bytes((("001.png", PNG + b"\0" * 5000),))
        small = PassthroughStore(
            env.engine, env.fsops, env.locate, env.data, max_received_bytes=1000
        )
        with pytest.raises(ReceivedTooLargeError):
            small.ingest(
                unit_id=env.chain["unit"], variant_id=env.chain["variant"],
                staged=env.stage(data), dest=str(env.manga / "x.cbz"),
            )  # fmt: skip
        assert env.files_under(env.library) == []


@pytest.mark.req("ENF-SEC-002")
def test_received_size_default_is_512_mib_and_settable() -> None:
    from shelfhold.config import load_settings

    base = {"SHELFHOLD_DATA_DIR": "/srv/d", "SHELFHOLD_STORE_DIR": "/srv/s"}
    assert load_settings(base).values.received_cbz_max_bytes == 512 * 1024 * 1024
    raised = load_settings({**base, "SHELFHOLD_RECEIVED_CBZ_MAX_BYTES": "1048576"})
    assert raised.values.received_cbz_max_bytes == 1048576


@pytest.mark.req("EF-BIB-006")
def test_ingest_refuses_to_overwrite_an_existing_library_file() -> None:
    with lib_env() as env:
        _, dest = ingest(env, cbz_bytes())
        before = dest.read_bytes()
        with pytest.raises(FileExistsError):
            store_for(env).ingest(
                unit_id=env.chain["unit"], variant_id=env.chain["variant"],
                staged=env.stage(cbz_bytes((("a.png", PNG),))), dest=str(dest),
            )  # fmt: skip
        assert dest.read_bytes() == before


@pytest.mark.req("EF-BIB-031")
@pytest.mark.parametrize(
    "principal",
    [
        admin(Credential.API_KEY),  # a key, whatever its scope
        admin(Credential.DEVICE_TOKEN),
        admin(age_s=3600),  # a session that has not re-entered its password lately
    ],
    ids=["api_key", "device_token", "stale_session"],
)
def test_unit_deletion_is_a_reserved_action_even_with_the_right_words(principal: Principal) -> None:
    with lib_env() as env:
        unit = env.chain["unit"]  # replaceable: no words needed, still refused
        with env.engine.connect() as c, pytest.raises(DeletionRefusedError):
            require_deletion_allowed(c, unit, principal=principal, now=NOW, typed=unit)
