"""L37: the full open export `shelfhold-export/1` and its import into a fresh instance (EF-INT-016,
EF-INT-014 `export-all`, EF-BIB-053 (5), EF-INT-027).

A real instance in process (migrated database, keyring, roots, lease, the production export
pipeline and the production purge): nothing is mocked but the simulated Kavita. Each "never" of the
lot is a test that tries to break it: a purged Work, a tombstone that no purge finished, a secret in
every table that holds one, a folder that is not ours, a caller that is not the admin, an archive
that lies."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings, load_settings
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import (
    acquisition,
    catalog,
    coherence,
    export,
    identity,
    reading,
    storage,
)  # fmt: skip
from shelfhold.infrastructure.schema import metadata as schema_metadata
from shelfhold.library.tombstones import (
    ContentHash,
    PlaceRef,
    SeriesBindingRef,
    TombstoneRoot,
    VariantRef,
    WorkRef,
)
from shelfhold.ops.errors import CodedRefusal, InvalidChoiceError
from shelfhold.ops.export_all import ExportAllOps
from shelfhold.platform.export_all import (
    DROPPED_COLUMNS,
    EXCLUDED_TABLES,
    EXPORTED_TABLES,
    FORMAT,
)
from tests.auth_env import run_cli
from tests.fakes.kavita import BASE_URL as KAVITA_URL
from tests.l41_env import KAVITA_PASSWORD, instance
from tests.l50_env import Seeded, Series, seeded

pytestmark = pytest.mark.req("EF-INT-016")

TITLE = "The Velvet Thorn Chronicle"
KEPT = "Berserk"
EDITION_KEY = (
    "work_id", "lang", "translation_type", "media", "publisher", "text_version", "performance",
    "packaging",
)  # fmt: skip
ARGON = "$argon2id$v=19$m=19456,t=2,p=1$c2FsdHNhbHQ$aGFzaGhhc2hoYXNo"
ADMIN = Principal("01J00000000000000000000ADM", "admin", "admin", Credential.LOCAL_CLI)
NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


def settings_in(root: Path) -> Settings:
    return load_settings(
        {
            "SHELFHOLD_DISK_PAUSE_PERCENT": "0",
            "SHELFHOLD_DISK_PAUSE_GIB": "0",
            "SHELFHOLD_DATA_DIR": str(root / "data"),
            "SHELFHOLD_STORE_DIR": str(root / "store"),
            "SHELFHOLD_LIBRARY_DIR": str(root / "library"),
        }
    ).values


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = settings_in(tmp_path)
    monkeypatch.setenv("SHELFHOLD_LIBRARY_DIR", str(settings.library_dir))
    monkeypatch.setenv("SHELFHOLD_DISK_PAUSE_PERCENT", "0")
    monkeypatch.setenv("SHELFHOLD_DISK_PAUSE_GIB", "0")
    return settings


def cli(monkeypatch: pytest.MonkeyPatch, settings: Settings, *argv: str) -> int:
    monkeypatch.setenv("SHELFHOLD_LIBRARY_DIR", str(settings.library_dir))
    return run_cli(monkeypatch, settings, *argv)


def everything_under(root: Path) -> str:
    """Every byte of every file below `root`, as text, with the names of the files in front."""
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        parts.append(path.relative_to(root).as_posix())
        if path.is_file():
            parts.append(path.read_bytes().decode("utf-8", errors="replace"))
    return "\n".join(parts)


def table_rows(root: Path, name: str) -> list[dict[str, Any]]:
    return json.loads((root / "data" / f"{name}.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def manifest(root: Path) -> dict[str, Any]:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@dataclass
class Shelved:
    kept: Series
    gone: Series
    kept_units: list[str]
    gone_units: list[str]


def shelve(env: Seeded) -> Shelved:
    """Two series on the shelf, each with two chapters received through the production store and
    written to the destination by the production pipeline."""
    kept, gone = env.new_series(KEPT), env.new_series(TITLE)
    # other pages: the same bytes as a purged chapter are covered by its tombstone, by design
    kept_units = [
        env.add_chapter(kept, n, page=(f"k{n}.png", b"\x89PNG\r\n\x1a\nkept" + n.encode()))
        for n in ("1", "2")
    ]
    gone_units = [env.add_chapter(gone, n) for n in ("1", "2")]
    env.export()
    return Shelved(kept, gone, kept_units, gone_units)


def identity_of(env: Seeded, unit: str) -> dict[str, Any]:
    """The values a tombstone cites for a Unit, as the purge records them."""
    u, v, b, i, e, c = (
        catalog.unit, catalog.unit_variant, acquisition.source_binding,
        acquisition.source_item, catalog.edition, storage.content,
    )  # fmt: skip
    with env.inst.engine.connect() as connection:
        row = connection.execute(
            sa.select(
                u.c.track,
                u.c.number_segment,
                u.c.number_raw,
                u.c.part,
                e.c.work_id,
                e.c.lang,
                e.c.translation_type,
                e.c.media,
                e.c.publisher,
                e.c.text_version,
                e.c.performance,
                e.c.packaging,
                v.c.remote_unit_id,
                i.c.plugin_id,
                i.c.source_key,
                i.c.remote_id,
                c.c.content_hash,
                c.c.content_hash_by,
            )
            .select_from(
                u.join(e, e.c.id == u.c.edition_id)
                .join(v, v.c.unit_id == u.c.id)
                .join(b, b.c.id == v.c.source_binding_id)
                .join(i, i.c.id == b.c.source_item_id)
                .join(c, c.c.variant_id == v.c.id)
            )
            .where(u.c.id == unit)
        ).one()
    return dict(row._mapping)


def tombstone(env: Seeded, values: Any) -> None:
    """A tombstone recorded the way a purge records it, with no row deleted: a purge that was cut
    short, or a database that came back from a backup taken before it."""
    _, loaded, service = env.inst.comp.read_context()
    roots = [TombstoneRoot(r.root_id, r.path, mounted=True) for r in loaded.roots]
    service.record(values, roots, reason="purge_unit")


def export_to(env: Seeded, folder: Path, principal: Principal = ADMIN) -> dict[str, Any]:
    return ExportAllOps(env.inst.comp).export_all(str(folder), principal=principal)


# --- the lot's red tests -----------------------------------------------------------------------


@pytest.mark.req("EF-BIB-053", "EF-INT-027")
def test_export_all_after_purge_contains_no_title(
    home: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real `purge --now`, then the real `export-all`: the title is nowhere, not in a row, not
    in a file name, not in a tombstone; what was not purged is there whole."""
    with seeded(home, tmp_path) as env:
        shelved = shelve(env)
        gone = shelved.gone.work_id
        assert cli(monkeypatch, home, "purge", "--now", "--work", gone, "--confirm", gone) == 0
        out = tmp_path / "out"
        capsys.readouterr()
        assert cli(monkeypatch, home, "export-all", str(out)) == 0
        assert str(out) in capsys.readouterr().out

        dumped = everything_under(out)
        assert TITLE not in dumped and TITLE.lower() not in dumped.lower()
        for unit in shelved.gone_units:
            assert unit not in dumped
        assert KEPT in dumped
        assert {r["id"] for r in table_rows(out, "unit")} == set(shelved.kept_units)
        for name in ("content_tombstone", "tombstone_kid", "tombstone_lost_kid"):
            assert not (out / "data" / f"{name}.json").exists()
        assert not any("tombstone" in p.name for p in out.rglob("*"))


@pytest.mark.req("EF-INT-027")
def test_export_all_schema_versioned_and_documented(home: Settings, tmp_path: Path) -> None:
    with seeded(home, tmp_path) as env:
        shelve(env)
        out = tmp_path / "out"
        export_to(env, out)
    described = manifest(out)
    assert described["format"] == described["schema_version"] == FORMAT == "shelfhold-export/1"
    assert set(described["tables"]) == set(EXPORTED_TABLES)  # every table, even an empty one
    for name, meta in described["tables"].items():
        assert meta["rows"] == len(table_rows(out, name))

    doc = Path(__file__).resolve().parents[3] / "docs/reference/format-export.md"
    text = doc.read_text(encoding="utf-8")
    assert FORMAT in text
    for name in sorted(EXPORTED_TABLES):  # a table added to the export is documented, or this fails
        assert f"`{name}`" in text, name
    for word in ("pierre tombale", "mot de passe", "instance neuve", "manifest.json"):
        assert word in text


@pytest.mark.req("EF-INT-016")
def test_open_format_export_roundtrip(
    home: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real scenario: chapters received, written to the shelf by the pipeline, profiles with
    their states and requests; exported; imported into a fresh instance that has only its roots.
    Every row and every file comes back identical; a second import is refused."""
    with seeded(home, tmp_path) as env:
        shelved = shelve(env)
        alice = fill_profiles(env, shelved)
        out = tmp_path / "out"
        report = export_to(env, out)
        before = dump(env.inst.engine)
        sources = {
            p.relative_to(out).as_posix(): p.read_bytes()
            for p in (out / "files").rglob("*")
            if p.is_file()
        }
    assert report["files"] >= 4 and sources, report  # the received archives of four chapters
    assert any(row["id"] == alice for row in table_rows(out, "user"))
    assert table_rows(out, "export_item") and table_rows(out, "unit_coverage")  # the shelf's own

    fresh_home = settings_in(tmp_path / "fresh")
    with fresh_instance(fresh_home, tmp_path / "fresh") as target:
        assert cli(monkeypatch, fresh_home, "import-all", str(out)) == 0
        assert dump(target.engine) == before

        restored = {
            p.relative_to(target.library.parent).as_posix(): p.read_bytes()
            for p in target.library.parent.rglob("*.cbz")
            if p.is_file()
        }
        assert {Path(r).name: b for r, b in restored.items()} == {
            Path(r).name: b for r, b in sources.items() if r.endswith(".cbz")
        }

        with target.engine.connect() as connection:  # no password came with the profiles
            hashes = connection.execute(sa.select(identity.user.c.password_hash)).scalars().all()
        assert hashes and all(h is None for h in hashes)
        assert cli(monkeypatch, fresh_home, "import-all", str(out)) != 0  # no second import
        assert dump(target.engine) == before
        audited = [r for r in rows(target.engine, identity.audit) if r.action == "import.all"]
        assert [a.actor_id.split(":")[0] for a in audited] == ["local_cli"]  # the host, by its uid
        assert audited[0].target == f"rows:{report['rows']};files:{report['files']}"


# --- what an export never carries ---------------------------------------------------------------


def test_every_table_is_either_exported_or_left_out_with_a_reason() -> None:
    names = {t.name for t in schema_metadata.sorted_tables}
    assert set(EXPORTED_TABLES) | set(EXCLUDED_TABLES) == names
    assert not set(EXPORTED_TABLES) & set(EXCLUDED_TABLES)
    assert all(reason for reason in EXCLUDED_TABLES.values())
    for name in ("content_tombstone", "tombstone_kid", "tombstone_lost_kid",
                 "tombstone_pending_root", "api_key", "session", "webauthn_credential",
                 "secret_key", "source_secret", "source_account", "source_config",
                 "export_target", "idempotency_key"):  # fmt: skip
        assert name in EXCLUDED_TABLES, name
    assert {"user": frozenset({"password_hash"})} == DROPPED_COLUMNS


def test_no_exported_table_holds_a_column_that_looks_like_a_secret() -> None:
    """A column named like a credential in a table the export carries is a decision to take, not
    a default: it is dropped, or this test is changed with its reason."""
    suspects = ("secret", "token", "password", "passphrase", "ciphertext", "nonce", "csrf", "key")
    found = {
        (t.name, c.name)
        for t in schema_metadata.sorted_tables
        if t.name in EXPORTED_TABLES
        for c in t.columns
        if any(s in c.name for s in suspects) and c.name not in DROPPED_COLUMNS.get(t.name, ())
    }
    assert found <= {
        ("unit", "follow_suppressed_by"),  # a profile's id
        ("source_item", "source_key"),  # the name of an upstream source, not a key
        ("profile_source_policy", "source_key"),
    }, found


def test_export_all_carries_no_secret(home: Settings, tmp_path: Path) -> None:
    """A marker in every place a secret lives, and the keyring on disk: none leaves."""
    marker = "c0ffee" * 10 + "c0de"  # 64 hex digits: a hash column takes it
    with seeded(home, tmp_path) as env:
        shelve(env)
        user = new_ulid()
        with env.inst.engine.begin() as connection:
            connection.execute(sa.insert(identity.user).values(
                id=user, username="alice", role="member",
                password_hash=f"$argon2id$v=19$m=19456,t=2,p=1${marker}$AAAA",
            ))  # fmt: skip
            connection.execute(sa.insert(identity.api_key).values(
                id=new_ulid(), user_id=user, prefix="shk_aaaaaaaa", scopes=["read"],
                hash=f"sha256:{marker}",
            ))  # fmt: skip
            connection.execute(sa.insert(identity.session).values(
                id=new_ulid(), user_id=user, token_hash=f"sha256:{marker}", csrf_secret=marker,
                last_seen_at=NOW, auth_time=NOW, absolute_expiry=NOW + timedelta(days=1),
            ))  # fmt: skip
            kid = connection.execute(sa.select(coherence.secret_key.c.kid)).scalars().first()
            assert kid is not None  # the keyring of the instance registered its key
            sealed = {
                "secret_kid": kid, "secret_v": 1, "secret_nonce": b"n" * 24,
                "secret_ct": marker.encode(),
            }  # fmt: skip
            connection.execute(sa.insert(acquisition.source_secret).values(
                id="p/secret/f/1", plugin_id="p", kind="secret", field="f", fingerprint=marker,
                generation=1, **sealed,
            ))  # fmt: skip
            connection.execute(
                sa.update(export.export_target).values(config={"k": marker}, **sealed)
            )
        keyring = home.data_dir / "keyring"
        assert keyring.exists()
        out = tmp_path / "out"
        export_to(env, out)
        dumped = everything_under(out)
        assert marker not in dumped
        assert "password_hash" not in dumped and "argon2id" not in dumped
        assert keyring.read_bytes().decode("latin-1")[:32] not in dumped
        assert not (out / "data" / "api_key.json").exists()
        assert not (out / "data" / "session.json").exists()
        assert [r["username"] for r in table_rows(out, "user")] == ["alice"]


@pytest.mark.parametrize("scope", ["variant", "place", "series_binding", "work", "hash"])
@pytest.mark.req("EF-BIB-053")
def test_export_all_leaves_out_what_a_tombstone_covers_even_when_no_purge_finished(
    home: Settings, tmp_path: Path, scope: str
) -> None:
    """The purge recorded the tombstone, then stopped before its rows went (or the database came
    back from a backup taken before it): the database still holds the Work, and the export still
    must not."""
    with seeded(home, tmp_path) as env:
        shelved = shelve(env)
        a, b = shelved.gone_units
        for unit in shelved.gone_units:  # a tombstone per scope, covering the whole series
            ident = identity_of(env, unit)
            hashes = (ContentHash(ident["content_hash_by"], ident["content_hash"]),)
            key = {k: ident[k] for k in EDITION_KEY}
            match scope:
                case "variant":
                    values: Any = VariantRef(
                        ident["plugin_id"], ident["source_key"], ident["remote_unit_id"], hashes
                    )
                case "place":
                    values = PlaceRef.of(
                        key, track=ident["track"], number_segment=ident["number_segment"],
                        number_raw=ident["number_raw"], part=ident["part"],
                    )  # fmt: skip
                case "series_binding":
                    values = SeriesBindingRef(
                        ident["plugin_id"], ident["source_key"], ident["remote_id"]
                    )
                case "hash":  # the very bytes, under another upstream reference
                    values = VariantRef("elsewhere", "elsewhere", "another-chapter", hashes)
                case _:
                    break
            tombstone(env, values)
            if scope == "series_binding":
                break
        if scope == "work":
            with env.inst.engine.begin() as connection:
                connection.execute(sa.insert(catalog.external_id).values(
                    entity_type="work", entity_id=shelved.gone.work_id, provider="isbn",
                    value="9780000000002", origin="manual",
                ))  # fmt: skip
            tombstone(env, WorkRef("isbn", "9780000000002"))
        assert {b, a} <= {r.id for r in env.rows(catalog.unit)}  # nothing was deleted

        out = tmp_path / "out"
        report = export_to(env, out)
        dumped = everything_under(out)
        assert TITLE not in dumped
        for unit in shelved.gone_units:
            assert unit not in dumped
        assert {r["id"] for r in table_rows(out, "unit")} == set(shelved.kept_units)
        assert shelved.gone.work_id not in {r["id"] for r in table_rows(out, "work")}
        assert shelved.gone.edition_id not in {r["id"] for r in table_rows(out, "edition")}
        assert {r["unit_id"] for r in manifest(out)["files"]} == set(shelved.kept_units)
        assert report["withheld_units"] == 2 and report["withheld_works"] == 1
        assert KEPT in dumped


def test_export_all_keeps_the_rest_of_a_work_when_one_chapter_is_covered(
    home: Settings, tmp_path: Path
) -> None:
    with seeded(home, tmp_path) as env:
        shelved = shelve(env)
        ident = identity_of(env, shelved.gone_units[0])
        tombstone(env, VariantRef(ident["plugin_id"], ident["source_key"], ident["remote_unit_id"]))
        out = tmp_path / "out"
        export_to(env, out)
        units = {r["id"] for r in table_rows(out, "unit")}
        assert shelved.gone_units[0] not in units and shelved.gone_units[1] in units
        assert TITLE in everything_under(out)  # the Work still has a chapter that is not covered
        files = {r["unit_id"] for r in manifest(out)["files"]}
        assert shelved.gone_units[0] not in files and shelved.gone_units[1] in files


def test_export_all_never_dangles_a_reference_to_what_it_left_out(
    home: Settings, tmp_path: Path
) -> None:
    """An Edition translated from one that a tombstone covers, a credit on a covered Work, a series
    that only held it: nothing points at what is not there, and the import accepts the rest."""
    with seeded(home, tmp_path) as env:
        shelved = shelve(env)
        with env.inst.engine.begin() as connection:
            connection.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == shelved.kept.edition_id)
                .values(translated_from_edition_id=shelved.gone.edition_id)
            )
            series, author = new_ulid(), new_ulid()
            connection.execute(
                sa.insert(catalog.series).values(id=series, title=TITLE, sort_title="t")
            )
            connection.execute(sa.insert(catalog.series_member).values(
                series_id=series, work_id=shelved.gone.work_id, position=1
            ))  # fmt: skip
            connection.execute(
                sa.insert(catalog.contributor).values(id=author, primary_name="Only Gone")
            )
            connection.execute(sa.insert(catalog.contribution).values(
                contributor_id=author, entity_type="work", entity_id=shelved.gone.work_id,
                role="aut"
            ))  # fmt: skip
        for unit in shelved.gone_units:
            ident = identity_of(env, unit)
            tombstone(
                env, SeriesBindingRef(ident["plugin_id"], ident["source_key"], ident["remote_id"])
            )
            break
        out = tmp_path / "out"
        export_to(env, out)
        edition = next(r for r in table_rows(out, "edition") if r["id"] == shelved.kept.edition_id)
        assert edition["translated_from_edition_id"] is None
        assert table_rows(out, "series") == [] and table_rows(out, "series_member") == []
        assert "Only Gone" not in everything_under(out)
        assert TITLE not in everything_under(out)


def test_export_all_says_what_it_could_not_copy(home: Settings, tmp_path: Path) -> None:
    with seeded(home, tmp_path) as env:
        shelved = shelve(env)
        received = next(
            p for p in (home.library_dir / "manga").rglob("*.cbz") if "received" in p.parts
        )
        received.write_bytes(received.read_bytes() + b"altered")  # no longer the registry's bytes
        out = tmp_path / "out"
        report = export_to(env, out)
        assert report["files_off_registry"] >= 1
        assert any(f["matches_registry"] is False for f in manifest(out)["files"])
        assert shelved.kept_units


# --- who may, and what is written -----------------------------------------------------------------


def principals() -> dict[str, Principal]:
    other = {"user_id": "01J00000000000000000000USR", "username": "x"}
    return {
        "member": Principal(role="member", credential=Credential.LOCAL_CLI, **other),
        "child": Principal(role="child", credential=Credential.LOCAL_CLI, **other),
        "api_key_admin": Principal(
            role="admin",
            credential=Credential.API_KEY,
            key_id="01J0000000000000000000KEY0",
            scopes=("admin",),
            **other,
        ),
        "device_token": Principal(
            role="admin",
            credential=Credential.DEVICE_TOKEN,
            key_id="01J0000000000000000000TOK0",
            **other,
        ),
        "stale_session": Principal(
            role="admin",
            credential=Credential.SESSION,
            session_id="01J0000000000000000000SES0",
            auth_time=datetime(2026, 1, 1, tzinfo=UTC),
            **other,
        ),
        "session_without_proof": Principal(
            role="admin",
            credential=Credential.SESSION,
            session_id="01J0000000000000000000SES1",
            **other,
        ),
    }


@pytest.mark.parametrize("who", sorted(principals()))
@pytest.mark.req("EF-USR-024")
def test_export_all_and_import_all_are_refused_to_whoever_is_not_the_admin_on_the_host(
    home: Settings, tmp_path: Path, who: str
) -> None:
    with seeded(home, tmp_path) as env:
        shelve(env)
        ops = ExportAllOps(env.inst.comp)
        out = tmp_path / "out"
        with pytest.raises(CodedRefusal) as refusal:
            ops.export_all(str(out), principal=principals()[who])
        assert refusal.value.code == "forbidden"
        assert not out.exists()  # not even the folder
        with pytest.raises(CodedRefusal):
            ops.import_all(str(out), principal=principals()[who])
        assert [r for r in rows(env.inst.engine, identity.audit) if r.action.endswith(".all")] == []


def test_export_all_of_the_admin_is_audited_without_a_title_or_a_folder(
    home: Settings, tmp_path: Path
) -> None:
    with seeded(home, tmp_path) as env:
        shelve(env)
        out = tmp_path / "somewhere-private-name"
        report = export_to(env, out)
        (audit,) = [r for r in rows(env.inst.engine, identity.audit) if r.action == "export.all"]
        assert audit.actor_id == ADMIN.actor_id
        assert audit.target == f"rows:{report['rows']};files:{report['files']}"
        assert TITLE not in audit.target and "private-name" not in audit.target


def test_export_all_refuses_when_a_root_is_away_because_its_tombstones_cannot_be_read(
    home: Settings, tmp_path: Path
) -> None:
    """A root that is not there is a journal that cannot be read: what its tombstones cover could
    not be left out, so nothing is exported."""
    with seeded(home, tmp_path) as env:
        shelve(env)
        _, loaded, _ = env.inst.comp.read_context()
        root = Path(next(iter(loaded.roots)).path)
        away = root.with_name(root.name + "-away")
        os.rename(root, away)
        root.mkdir()
        try:
            out = tmp_path / "out"
            with pytest.raises(CodedRefusal) as refusal:
                export_to(env, out)
            assert refusal.value.code == "root.blocked"
            assert not out.exists()
        finally:
            root.rmdir()
            os.rename(away, root)


def test_export_all_folder_rules(home: Settings, tmp_path: Path) -> None:
    with seeded(home, tmp_path) as env:
        shelve(env)
        busy = tmp_path / "busy"
        busy.mkdir()
        (busy / "mine.txt").write_text("keep me", encoding="utf-8")
        with pytest.raises(InvalidChoiceError, match="not_empty"):
            export_to(env, busy)
        assert [p.name for p in busy.iterdir()] == ["mine.txt"]  # nothing touched, nothing removed
        assert (busy / "mine.txt").read_text(encoding="utf-8") == "keep me"

        file = tmp_path / "a-file"
        file.write_text("x", encoding="utf-8")
        with pytest.raises(InvalidChoiceError, match="not_a_folder"):
            export_to(env, file)
        link = tmp_path / "link"
        link.symlink_to(busy, target_is_directory=True)
        with pytest.raises(InvalidChoiceError, match="is_link"):
            export_to(env, link)
        with pytest.raises(InvalidChoiceError, match="no_parent"):
            export_to(env, tmp_path / "missing" / "out")
        for banned in (home.data_dir / "export", home.store_dir / "export",
                       home.library_dir / "manga" / "export", home.data_dir):  # fmt: skip
            before = (
                sorted(p.name for p in banned.parent.iterdir()) if banned.parent.exists() else []
            )
            with pytest.raises(InvalidChoiceError, match=r"inside_root|not_empty"):
                export_to(env, banned)
            after = (
                sorted(p.name for p in banned.parent.iterdir()) if banned.parent.exists() else []
            )
            assert before == after

        empty = tmp_path / "empty"
        empty.mkdir()
        export_to(env, empty)  # an empty folder is fine
        assert (empty / "manifest.json").is_file()


def test_export_all_writes_owner_only_and_overwrites_nothing(
    home: Settings, tmp_path: Path
) -> None:
    with seeded(home, tmp_path) as env:
        shelve(env)
        out = tmp_path / "out"
        export_to(env, out)
        for path in out.rglob("*"):
            mode = stat.S_IMODE(path.lstat().st_mode)
            assert mode & 0o077 == 0, (path, oct(mode))
        assert not [p for p in out.rglob("*") if p.is_symlink()]
        with pytest.raises(InvalidChoiceError, match="not_empty"):
            export_to(env, out)  # the second run finds the first one and leaves it as it is


def test_a_half_written_export_has_no_manifest_and_is_not_imported(
    home: Settings, tmp_path: Path
) -> None:
    with seeded(home, tmp_path) as env:
        shelve(env)
        out = tmp_path / "out"
        export_to(env, out)
    (out / "manifest.json").unlink()
    refused = import_into_fresh(tmp_path, out)
    assert refused.reason == "no_manifest"


# --- the import ---------------------------------------------------------------------------------


def rows(engine: sa.Engine, table: sa.Table) -> list[sa.Row[tuple[object, ...]]]:
    with engine.connect() as connection:
        return list(connection.execute(sa.select(table)))


def _plain(value: object) -> str:
    """A value as text; a JSON document by its content, not by the order its keys were set in."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return repr(value)


def dump(engine: sa.Engine) -> dict[str, list[dict[str, str]]]:
    """Every row of every exported table, as text, in a stable order (the dropped columns left)."""
    result: dict[str, list[dict[str, str]]] = {}
    with engine.connect() as connection:
        for table in schema_metadata.sorted_tables:
            if table.name not in EXPORTED_TABLES:
                continue
            dropped = DROPPED_COLUMNS.get(table.name, frozenset())
            names = [c.name for c in table.columns if c.name not in dropped]
            found = [
                {n: _plain(row._mapping[n]) for n in names}
                for row in connection.execute(sa.select(*(table.c[n] for n in names)))
            ]
            result[table.name] = sorted(found, key=lambda r: json.dumps(r, sort_keys=True))
    return result


def fill_profiles(env: Seeded, shelved: Shelved) -> str:
    """Two profiles with their states, requests and reading positions, and the rest of a catalog."""
    alice, bob = new_ulid(), new_ulid()
    unit = shelved.kept_units[0]
    with env.inst.engine.begin() as connection:
        content = connection.execute(
            sa.select(storage.content.c.id, storage.content.c.content_hash).where(
                storage.content.c.unit_id == unit
            )
        ).one()
        connection.execute(sa.insert(identity.user).values(
            id=alice, username="alice", role="member",
            password_hash=ARGON,
        ))  # fmt: skip
        connection.execute(sa.insert(identity.user).values(
            id=bob, username="bob", role="child", max_age_rating=12, locale="en",
            title_lang_pref=["fr", "en"],
        ))  # fmt: skip
        for who, work in ((alice, shelved.kept.work_id), (bob, shelved.kept.work_id)):
            connection.execute(sa.insert(identity.user_state).values(
                user_id=who, work_id=work, followed=True, favorite=who == alice, read_at=NOW,
            ))  # fmt: skip
        connection.execute(sa.insert(identity.request).values(
            id=new_ulid(), requester_user_id=bob, target_kind="free_text",
            target_value="Another series", state="pending",
        ))  # fmt: skip
        connection.execute(sa.insert(identity.request).values(
            id=new_ulid(), requester_user_id=alice, target_kind="work_id",
            target_value=shelved.kept.work_id, state="approved",
        ))  # fmt: skip
        connection.execute(sa.insert(reading.reading_position).values(
            id=new_ulid(), user_id=alice, carrier_type="content", carrier_id=content.id,
            content_hash=content.content_hash, locator={"page": 3}, percent=0.5, client_ts=NOW,
        ))  # fmt: skip
        connection.execute(sa.insert(identity.profile_source_policy).values(
            user_id=bob, source_key="suwayomi", policy="forbidden",
        ))  # fmt: skip
        author, series = new_ulid(), new_ulid()
        connection.execute(
            sa.insert(catalog.contributor).values(id=author, primary_name="Kentaro Miura")
        )
        connection.execute(sa.insert(catalog.contributor_name).values(
            contributor_id=author, name="三浦 建太郎",
        ))  # fmt: skip
        connection.execute(sa.insert(catalog.contribution).values(
            contributor_id=author, entity_type="work", entity_id=shelved.kept.work_id, role="aut",
        ))  # fmt: skip
        connection.execute(
            sa.insert(catalog.series).values(id=series, title="Dark Fantasy", sort_title="dark")
        )
        connection.execute(sa.insert(catalog.series_member).values(
            series_id=series, work_id=shelved.kept.work_id, position=1, label="Tome 1",
        ))  # fmt: skip
        connection.execute(sa.insert(catalog.external_id).values(
            entity_type="work", entity_id=shelved.kept.work_id, provider="isbn",
            value="9780000000019", origin="manual",
        ))  # fmt: skip
        connection.execute(sa.insert(catalog.metadata_field).values(
            entity_type="work", entity_id=shelved.kept.work_id, field="description", lang="fr",
            value="Une description écrite à la main", provider="manual", provenance="manual",
            fetched_at=NOW, locked=True,
        ))  # fmt: skip
    return alice


@contextmanager
def fresh_instance(settings: Settings, root: Path) -> Iterator[Any]:
    """An instance that has its roots (a target was added) and nothing else."""
    with instance(settings, root) as inst:
        inst.admin.target_add_kavita(
            name="k2", url=KAVITA_URL, username="u", password=KAVITA_PASSWORD,
            accept_admin_risk=True,
            path_map=[{"local": str(inst.library), "remote": "/manga", "mode": "rw"}],
        )  # fmt: skip
        yield inst


def import_into_fresh(tmp_path: Path, source: Path) -> Any:
    from shelfhold.platform.import_all import ImportRefusedError

    fresh = settings_in(tmp_path / "fresh-for-refusal")
    with fresh_instance(fresh, tmp_path / "fresh-for-refusal") as target:
        ops = ExportAllOps(target.comp)
        with pytest.raises(InvalidChoiceError) as refused:
            ops.import_all(str(source), principal=ADMIN)
        assert not any(rows(target.engine, t) for t in _exported(target))
        assert [f for f in fresh.library_dir.rglob("*.cbz")] == []
        cause = refused.value.__cause__
        assert isinstance(cause, ImportRefusedError), refused.value
        return cause


def _exported(target: Any) -> list[sa.Table]:
    return [t for t in schema_metadata.sorted_tables if t.name in EXPORTED_TABLES]


def tamper(out: Path, how: str) -> None:
    def edit(rel: str, change: Any) -> None:
        path = out / rel
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    match how:
        case "table_changed":  # a row added after the digest was taken
            edit("data/work.json", lambda d: d.append(dict(d[0], id=new_ulid())))
        case "version":
            future = {"format": "shelfhold-export/2", "schema_version": "shelfhold-export/2"}
            edit("manifest.json", lambda d: d.update(future))
        case "unknown_table":
            (out / "data" / "api_key.json").write_text("[]", encoding="utf-8")
            edit("manifest.json", lambda d: d["tables"].update(api_key={"rows": 0, "sha256": "x"}))
        case "password_hash":
            data = json.loads((out / "data" / "user.json").read_text(encoding="utf-8"))
            data[0]["password_hash"] = ARGON
            raw = json.dumps(data)
            (out / "data" / "user.json").write_text(raw, encoding="utf-8")
            import hashlib

            edit("manifest.json", lambda d: d["tables"]["user"].update(
                sha256="sha256:" + hashlib.sha256(raw.encode()).hexdigest()))  # fmt: skip
        case "traversal":
            edit("manifest.json", lambda d: d["files"][0].update(path="../../outside.cbz"))
        case "absolute":
            edit("manifest.json", lambda d: d["files"][0].update(file="/etc/passwd"))
        case "file_changed":
            first = next((out / "files").rglob("*.cbz"))
            first.write_bytes(first.read_bytes() + b"x")
        case "file_removed":
            next((out / "files").rglob("*.cbz")).unlink()
        case "unknown_root":
            edit("manifest.json", lambda d: d["files"][0].update(root="nowhere"))
        case "file_link":
            first = next((out / "files").rglob("*.cbz"))
            data = first.read_bytes()
            first.unlink()
            elsewhere = out.parent / "elsewhere.cbz"
            elsewhere.write_bytes(data)
            first.symlink_to(elsewhere)
        case _:
            raise AssertionError(how)


@pytest.mark.parametrize(
    ("how", "reason"),
    [
        ("table_changed", "digest_mismatch"),
        ("version", "incompatible_version"),
        ("unknown_table", "unknown_table"),
        ("password_hash", "unknown_column"),
        ("traversal", "bad_path"),
        ("absolute", "bad_path"),
        ("file_changed", "missing_file"),
        ("file_removed", "missing_file"),
        ("unknown_root", "no_root"),
        ("file_link", "missing_file"),
    ],
)
def test_import_all_refuses_an_archive_that_lies_and_leaves_the_instance_empty(
    home: Settings, tmp_path: Path, how: str, reason: str
) -> None:
    with seeded(home, tmp_path) as env:
        shelved = shelve(env)
        fill_profiles(env, shelved)
        out = tmp_path / "out"
        export_to(env, out)
    tamper(out, how)
    assert import_into_fresh(tmp_path, out).reason == reason


def test_import_all_refuses_an_instance_that_is_not_empty_whatever_it_holds(
    home: Settings, tmp_path: Path
) -> None:
    """One profile, one shelf, one Work: any of them is enough to refuse, and nothing is merged."""
    with seeded(home, tmp_path) as env:
        shelved = shelve(env)
        fill_profiles(env, shelved)
        out = tmp_path / "out"
        export_to(env, out)
        before = dump(env.inst.engine)
        with pytest.raises(InvalidChoiceError, match="not_empty"):  # the very instance it came from
            ExportAllOps(env.inst.comp).import_all(str(out), principal=ADMIN)
        assert dump(env.inst.engine) == before

    for name, table, values in (
        ("a profile", identity.user, {"id": new_ulid(), "username": "zed", "role": "member"}),
        ("a shelf", export.shelf, {"id": new_ulid(), "name": "S", "phrase": "p"}),
    ):
        fresh_home = settings_in(tmp_path / name.replace(" ", "-"))
        with fresh_instance(fresh_home, tmp_path / name.replace(" ", "-")) as target:
            with target.engine.begin() as connection:
                connection.execute(sa.insert(table).values(**values))
            with pytest.raises(InvalidChoiceError, match="not_empty"):
                ExportAllOps(target.comp).import_all(str(out), principal=ADMIN)
            assert len(rows(target.engine, table)) == 1, name
            assert not rows(target.engine, catalog.work)


def test_import_all_rolls_back_every_row_when_one_is_refused(
    home: Settings, tmp_path: Path
) -> None:
    """A row the schema refuses, late in the order: the earlier tables are not left behind."""
    import hashlib

    with seeded(home, tmp_path) as env:
        shelved = shelve(env)
        fill_profiles(env, shelved)
        out = tmp_path / "out"
        export_to(env, out)
    path = out / "data" / "request.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data[0]["state"] = "no-such-state"  # a CHECK constraint of the schema
    raw = json.dumps(data)
    path.write_text(raw, encoding="utf-8")
    described = manifest(out)
    described["tables"]["request"]["sha256"] = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
    (out / "manifest.json").write_text(json.dumps(described), encoding="utf-8")
    assert import_into_fresh(tmp_path, out).reason == "rows_rejected"


def test_import_all_in_a_dangling_archive_is_refused(home: Settings, tmp_path: Path) -> None:
    import hashlib

    with seeded(home, tmp_path) as env:
        shelve(env)
        out = tmp_path / "out"
        export_to(env, out)
    path = out / "data" / "unit.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data[0]["edition_id"] = new_ulid()  # an Edition that is nowhere
    raw = json.dumps(data)
    path.write_text(raw, encoding="utf-8")
    described = manifest(out)
    described["tables"]["unit"]["sha256"] = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
    (out / "manifest.json").write_text(json.dumps(described), encoding="utf-8")
    assert import_into_fresh(tmp_path, out).reason == "dangling_reference"


# --- the commands -------------------------------------------------------------------------------


def test_cli_export_all_and_import_all_say_what_they_did(
    home: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with seeded(home, tmp_path) as env:
        shelve(env)
        gone = env.rows(catalog.work)[-1].id
        assert cli(monkeypatch, home, "purge", "--now", "--work", gone, "--confirm", gone) == 0
        out = tmp_path / "out"
        capsys.readouterr()
        assert cli(monkeypatch, home, "export-all", str(out)) == 0
        printed = capsys.readouterr().out
        assert "fichiers" in printed and "lignes" in printed
        assert cli(monkeypatch, home, "export-all", str(out)) != 0  # the folder is no longer empty
        assert "not_empty" in capsys.readouterr().err
    fresh_home = settings_in(tmp_path / "fresh")
    with fresh_instance(fresh_home, tmp_path / "fresh"):
        capsys.readouterr()
        assert cli(monkeypatch, fresh_home, "import-all", str(out)) == 0
        printed = capsys.readouterr().out
        assert "Import terminé" in printed and "reset-password" in printed


def test_the_commands_are_wired_to_a_reserved_action_and_the_server_socket() -> None:
    from shelfhold.identity.public import RESERVED_ACTIONS, Effect
    from shelfhold.interfaces.local_cli.ops import OPS_METHODS

    for method, action in (("ops.export_all", "export.all"), ("ops.import_all", "import.all")):
        assert OPS_METHODS[method][1] == action
        assert RESERVED_ACTIONS[action].effects == {Effect.MOVES_INSTANCE_DATA}


def row_counts(engine: sa.Engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            t.name: connection.execute(sa.select(sa.func.count()).select_from(t)).scalar_one()
            for t in schema_metadata.sorted_tables
            if t.name not in ("audit", "event")
        }


def test_the_export_only_reads_the_database(home: Settings, tmp_path: Path) -> None:
    """Nothing of the database changes under an export but its own audit entry."""
    with seeded(home, tmp_path) as env:
        shelve(env)
        before, counts = dump(env.inst.engine), row_counts(env.inst.engine)
        export_to(env, tmp_path / "out")
        assert dump(env.inst.engine) == before
        assert row_counts(env.inst.engine) == counts
