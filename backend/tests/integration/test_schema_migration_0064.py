"""Migration 0064 (M4 L02): the schema the lots of M4 share.

`tree` and `tree_indexer` (ADR-0052), `destination_account`, `progress_sync_cursor`, an alias per
binding on `document_alias`, one link per shelf and remote library. The DDL parity of the whole
database is `test_migrated_ddl_equals_create_all` (0002 module): it runs at head and covers this
revision. The tests below attack each guard that SQL can state; T1 to T11 are the application's.
"""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import default_script_location, migrate
from shelfhold.infrastructure.schema import metadata
from tests.integration.test_schema_migration_0002 import alembic_config, tables
from tests.integration.test_schema_migration_0046 import add_row, add_user
from tests.integration.test_schema_catalog import add_edition, add_work

M4_TABLES = {"tree", "tree_indexer", "destination_account", "progress_sync_cursor"}
HASH = "sha256:" + "ab" * 32
MD5 = "md5:" + "cd" * 16


@pytest.fixture
def db(settings: Settings) -> Iterator[sqlite3.Connection]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    connection = sqlite3.connect(settings.database_path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    yield connection
    connection.close()


def refused(db: sqlite3.Connection, table: str, fragment: str, **values: Any) -> None:
    """The insert fails in the database, for the reason named (a constraint or a message)."""
    with pytest.raises(sqlite3.DatabaseError) as raised:
        add_row(db, table, **values)
    assert fragment in str(raised.value), raised.value


def sealed(kid: str) -> dict[str, Any]:
    """A sealed envelope made of random bytes: no secret, literal or not, is ever in a test."""
    return {
        "secret_kid": kid,
        "secret_v": 1,
        "secret_nonce": os.urandom(12),
        "secret_ct": os.urandom(32),
        "token_fingerprint": new_ulid()[-12:],
    }


def key(db: sqlite3.Connection) -> str:
    kid = f"k-{new_ulid()[-8:]}"
    add_row(db, "secret_key", kid=kid, state="active")
    return kid


def root(db: sqlite3.Connection) -> str:
    root_id = new_ulid()
    add_row(db, "physical_root", root_id=root_id, path_entry=f"/r/{root_id}")
    return root_id


def target(db: sqlite3.Connection) -> str:
    target_id = new_ulid()
    add_row(db, "export_target", id=target_id, kind="kavita", name=f"k-{target_id}")
    return target_id


def shelf(db: sqlite3.Connection) -> str:
    shelf_id = new_ulid()
    add_row(db, "shelf", id=shelf_id, name=f"s-{shelf_id}", phrase="Manga")
    return shelf_id


def destination(db: sqlite3.Connection, shelf_id: str, target_id: str, **extra: Any) -> str:
    destination_id = new_ulid()
    add_row(
        db, "shelf_destination", id=destination_id, shelf_id=shelf_id, target_id=target_id,
        transport=extra.pop("transport", "tree"), egress="foyer",
        root_subpath=extra.pop("root_subpath", f"m-{destination_id}"),
        template="{series}", template_origin="default@1", **extra,
    )  # fmt: skip
    return destination_id


def tree(db: sqlite3.Connection, root_id: str, subpath: str = "manga") -> str:
    tree_id = new_ulid()
    add_row(db, "tree", id=tree_id, root_id=root_id, root_subpath=subpath)
    return tree_id


def indexer(
    db: sqlite3.Connection, tree_id: str, dest: str, target_id: str, role: str = "primary"
) -> None:
    add_row(
        db, "tree_indexer", tree_id=tree_id, shelf_destination_id=dest, target_id=target_id,
        role=role,
    )  # fmt: skip


@pytest.mark.req("ENF-OPS-002")
def test_single_alembic_head_is_0064_on_0063() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert len(script.get_heads()) == 1
    assert script.get_revision("0064").down_revision == "0063"


@pytest.mark.req("ENF-OPS-002", "EF-EXP-002")
def test_metadata_covers_the_m4_tables(db: sqlite3.Connection, settings: Settings) -> None:
    assert tables(settings.database_path) >= M4_TABLES
    assert {t.name for t in metadata.sorted_tables} >= M4_TABLES


@pytest.mark.req("ENF-OPS-002", "EF-EXP-002", "ENF-QUAL-005")
def test_upgrade_from_0063_keeps_data(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0063")
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        shelf_id, target_id = shelf(conn), target(conn)
        dest = destination(conn, shelf_id, target_id)
        library = new_ulid()
        add_row(conn, "remote_library", id=library, export_target_id=target_id, remote_ref="1")
        add_row(
            conn, "shelf_destination_remote_library", shelf_destination_id=dest,
            export_target_id=target_id, remote_library_id=library,
        )  # fmt: skip
        work_id = add_work_row(conn)
        item = new_ulid()
        add_row(conn, "export_item", id=item, edition_id=edition_row(conn, work_id), kind="unit")
        add_row(
            conn, "document_alias", export_item_id=item, file_hash=HASH, content_hash=HASH,
            koreader_partial_md5=MD5,
        )  # fmt: skip
    assert "0064" in migrate(settings).applied
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT count(*) FROM shelf_destination").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM shelf_destination_remote_library").fetchone() == (
            1,
        )
        row = conn.execute(
            "SELECT file_hash, koreader_partial_md5, kavita_md5, shelf_destination_id "
            "FROM document_alias"
        ).fetchone()
        assert row == (HASH, MD5, None, None)  # the old rewrites stay, bound to no destination
        assert conn.execute("SELECT count(*) FROM tree").fetchone() == (0,)


def add_work_row(conn: sqlite3.Connection) -> str:
    work_id = new_ulid()
    conn.execute("INSERT OR IGNORE INTO work_kind (key, label_fr) VALUES ('manga', 'Manga')")
    add_row(conn, "work", id=work_id, kind="manga", title="t", sort_title="t")
    return work_id


def edition_row(conn: sqlite3.Connection, work_id: str) -> str:
    edition = new_ulid()
    add_row(
        conn, "edition", id=edition, work_id=work_id, lang="en", publisher="", text_version="",
        performance="", packaging="",
    )  # fmt: skip
    return edition


@pytest.mark.req("ENF-OPS-002")
def test_downgrade_to_0063_drops_the_m4_schema(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    command.downgrade(alembic_config(database), "0063")
    assert not (M4_TABLES & tables(database))
    with closing(sqlite3.connect(database)) as conn:
        columns = {r[1] for r in conn.execute('PRAGMA table_info("document_alias")')}
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert not ({"kavita_md5", "shelf_destination_id"} & columns)
    assert not {t for t in triggers if t.startswith(("tree_indexer_", "link_one_library"))}


# --------------------------------------------------------------------------- tree


@pytest.mark.req("EF-EXP-002", "EF-EXP-058")
def test_tree_unique_per_root_and_subpath(db: sqlite3.Connection) -> None:
    root_id = root(db)
    tree(db, root_id, "manga")
    tree(db, root(db), "manga")  # another root: another tree
    tree(db, root_id, "")  # the root itself
    refused(db, "tree", "UNIQUE constraint failed", id=new_ulid(), root_id=root_id,
            root_subpath="manga")  # fmt: skip


@pytest.mark.req("EF-EXP-002")
def test_tree_needs_a_real_root_and_a_relative_subpath(db: sqlite3.Connection) -> None:
    refused(db, "tree", "FOREIGN KEY constraint failed", id=new_ulid(), root_id=new_ulid(),
            root_subpath="m")  # fmt: skip
    refused(db, "tree", "ck_tree_root_subpath_relative", id=new_ulid(), root_id=root(db),
            root_subpath="/abs")  # fmt: skip
    refused(db, "tree", "ck_tree_id_ulid", id="nope", root_id=root(db), root_subpath="m")
    refused(db, "tree", "ck_tree_render_profile_object", id=new_ulid(), root_id=root(db),
            root_subpath="m", render_profile="[]")  # fmt: skip


@pytest.mark.req("EF-EXP-002")
def test_a_physical_root_with_a_tree_is_not_deleted_silently(db: sqlite3.Connection) -> None:
    root_id = root(db)
    tree(db, root_id)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM physical_root WHERE root_id = ?", (root_id,))


# ---------------------------------------------------------------------- tree_indexer


@pytest.mark.req("EF-EXP-002", "EF-EXP-058")
def test_indexer_role_is_primary_or_secondary(db: sqlite3.Connection) -> None:
    shelf_id, target_id = shelf(db), target(db)
    dest = destination(db, shelf_id, target_id)
    refused(db, "tree_indexer", "ck_tree_indexer_role_values", tree_id=tree(db, root(db)),
            shelf_destination_id=dest, target_id=target_id, role="owner")  # fmt: skip


@pytest.mark.req("EF-EXP-002", "EF-EXP-058")
def test_a_binding_belongs_to_one_tree(db: sqlite3.Connection) -> None:
    shelf_id, target_id = shelf(db), target(db)
    dest = destination(db, shelf_id, target_id)
    root_id = root(db)
    indexer(db, tree(db, root_id, "a"), dest, target_id)
    refused(db, "tree_indexer", "UNIQUE constraint failed", tree_id=tree(db, root_id, "b"),
            shelf_destination_id=dest, target_id=target_id, role="primary")  # fmt: skip


@pytest.mark.req("EF-EXP-002", "EF-EXP-058")
def test_one_primary_per_tree(db: sqlite3.Connection) -> None:
    shelf_id = shelf(db)
    t1, t2 = target(db), target(db)
    tree_id = tree(db, root(db))
    indexer(db, tree_id, destination(db, shelf_id, t1), t1, "primary")
    indexer(db, tree_id, destination(db, shelf_id, t2), t2, "secondary")  # any number of those
    t3 = target(db)
    refused(db, "tree_indexer", "UNIQUE constraint failed", tree_id=tree_id,
            shelf_destination_id=destination(db, shelf_id, t3), target_id=t3,
            role="primary")  # fmt: skip


@pytest.mark.req("EF-EXP-002", "EF-EXP-058")
def test_one_indexer_per_server_instance_and_tree(db: sqlite3.Connection) -> None:
    """T9: the same server does not index a tree twice."""
    shelf_id, target_id = shelf(db), target(db)
    tree_id = tree(db, root(db))
    indexer(db, tree_id, destination(db, shelf_id, target_id), target_id, "primary")
    refused(db, "tree_indexer", "UNIQUE constraint failed", tree_id=tree_id,
            shelf_destination_id=destination(db, shelf_id, target_id), target_id=target_id,
            role="secondary")  # fmt: skip


@pytest.mark.req("EF-EXP-002")
def test_indexer_target_is_the_one_of_its_binding(db: sqlite3.Connection) -> None:
    shelf_id, target_id = shelf(db), target(db)
    dest = destination(db, shelf_id, target_id)
    refused(db, "tree_indexer", "FOREIGN KEY constraint failed", tree_id=tree(db, root(db)),
            shelf_destination_id=dest, target_id=target(db), role="primary")  # fmt: skip


@pytest.mark.req("EF-EXP-002", "EF-EXP-058")
def test_indexers_of_a_tree_belong_to_one_shelf(db: sqlite3.Connection) -> None:
    """T2: a second shelf on the same tree stays refused."""
    t1, t2 = target(db), target(db)
    tree_id = tree(db, root(db))
    indexer(db, tree_id, destination(db, shelf(db), t1), t1, "primary")
    other = destination(db, shelf(db), t2)
    refused(db, "tree_indexer", "same shelf", tree_id=tree_id, shelf_destination_id=other,
            target_id=t2, role="secondary")  # fmt: skip


@pytest.mark.req("EF-EXP-002", "EF-EXP-058")
@pytest.mark.parametrize("transport", ["dropbox", "push", "pull"])
def test_only_a_tree_transport_has_a_tree(db: sqlite3.Connection, transport: str) -> None:
    target_id = target(db)
    dest = destination(db, shelf(db), target_id, transport=transport)
    refused(db, "tree_indexer", "tree transport", tree_id=tree(db, root(db)),
            shelf_destination_id=dest, target_id=target_id, role="primary")  # fmt: skip


@pytest.mark.req("EF-EXP-002", "EF-EXP-058")
def test_remove_primary_with_secondaries_refused(db: sqlite3.Connection) -> None:
    shelf_id = shelf(db)
    t1, t2 = target(db), target(db)
    tree_id = tree(db, root(db))
    primary, secondary = destination(db, shelf_id, t1), destination(db, shelf_id, t2)
    indexer(db, tree_id, primary, t1, "primary")
    indexer(db, tree_id, secondary, t2, "secondary")
    with pytest.raises(sqlite3.DatabaseError, match="secondaries"):
        db.execute("DELETE FROM tree_indexer WHERE shelf_destination_id = ?", (primary,))
    db.execute("DELETE FROM tree_indexer WHERE shelf_destination_id = ?", (secondary,))  # detach
    db.execute("DELETE FROM tree_indexer WHERE shelf_destination_id = ?", (primary,))  # then it


@pytest.mark.req("EF-EXP-002", "EF-EXP-058")
def test_deleting_the_tree_takes_its_indexers_with_it(db: sqlite3.Connection) -> None:
    shelf_id = shelf(db)
    t1, t2 = target(db), target(db)
    tree_id = tree(db, root(db))
    indexer(db, tree_id, destination(db, shelf_id, t1), t1, "primary")
    indexer(db, tree_id, destination(db, shelf_id, t2), t2, "secondary")
    db.execute("DELETE FROM tree WHERE id = ?", (tree_id,))
    assert db.execute("SELECT count(*) FROM tree_indexer").fetchone() == (0,)


@pytest.mark.req("EF-EXP-002")
def test_a_binding_in_a_tree_is_not_deleted_silently(db: sqlite3.Connection) -> None:
    target_id = target(db)
    dest = destination(db, shelf(db), target_id)
    indexer(db, tree(db, root(db)), dest, target_id)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM shelf_destination WHERE id = ?", (dest,))


@pytest.mark.req("EF-EXP-002")
def test_promotion_swaps_roles_without_touching_anything_else(db: sqlite3.Connection) -> None:
    shelf_id = shelf(db)
    t1, t2 = target(db), target(db)
    tree_id = tree(db, root(db))
    a, b = destination(db, shelf_id, t1), destination(db, shelf_id, t2)
    indexer(db, tree_id, a, t1, "primary")
    indexer(db, tree_id, b, t2, "secondary")
    db.execute("UPDATE tree_indexer SET role = 'secondary' WHERE shelf_destination_id = ?", (a,))
    db.execute("UPDATE tree_indexer SET role = 'primary' WHERE shelf_destination_id = ?", (b,))
    assert dict(db.execute("SELECT shelf_destination_id, role FROM tree_indexer")) == {
        a: "secondary", b: "primary",
    }  # fmt: skip


# ----------------------------------------------------------- shelf_destination links


@pytest.mark.req("EF-EXP-026")
def test_a_shelf_has_several_destinations_but_links_a_remote_library_once(
    db: sqlite3.Connection,
) -> None:
    shelf_id, target_id = shelf(db), target(db)
    first = destination(db, shelf_id, target_id)
    second = destination(db, shelf_id, target_id)  # N bindings per shelf
    library = new_ulid()
    add_row(db, "remote_library", id=library, export_target_id=target_id, remote_ref="1")
    link = {"export_target_id": target_id, "remote_library_id": library}
    add_row(db, "shelf_destination_remote_library", shelf_destination_id=first, **link)
    refused(db, "shelf_destination_remote_library", "already linked", shelf_destination_id=second,
            **link)  # fmt: skip
    # another shelf may link it: the shelves are independent (the dedicated folder is T6's)
    add_row(db, "shelf_destination_remote_library",
            shelf_destination_id=destination(db, shelf(db), target_id), **link)  # fmt: skip


@pytest.mark.req("EF-EXP-026")
def test_moving_a_link_onto_a_library_of_the_shelf_is_refused_too(db: sqlite3.Connection) -> None:
    shelf_id, target_id = shelf(db), target(db)
    first, second = destination(db, shelf_id, target_id), destination(db, shelf_id, target_id)
    one, two = new_ulid(), new_ulid()
    for library, ref in ((one, "1"), (two, "2")):
        add_row(db, "remote_library", id=library, export_target_id=target_id, remote_ref=ref)
    for dest, library in ((first, one), (second, two)):
        add_row(db, "shelf_destination_remote_library", shelf_destination_id=dest,
                export_target_id=target_id, remote_library_id=library)  # fmt: skip
    with pytest.raises(sqlite3.DatabaseError, match="already linked"):
        db.execute(
            "UPDATE shelf_destination_remote_library SET remote_library_id = ? "
            "WHERE shelf_destination_id = ?", (one, second),
        )


# ------------------------------------------------------------- destination_account


def account(db: sqlite3.Connection, user_id: str, dest: str, **extra: Any) -> None:
    values: dict[str, Any] = {
        "user_id": user_id, "shelf_destination_id": dest, "remote_user": f"r-{user_id[-6:]}",
        **extra,
    }  # fmt: skip
    if "secret_ct" not in values and "revoked_at" not in values:
        values.update(sealed(key(db)))
    add_row(db, "destination_account", **values)


@pytest.mark.req("EF-USR-012", "ENF-SEC-005")
def test_destination_account_unique_per_user_and_destination(db: sqlite3.Connection) -> None:
    dest = destination(db, shelf(db), target(db))
    user = add_user(db)
    account(db, user, dest)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        account(db, user, dest, remote_user="someone-else")
    account(db, user, destination(db, shelf(db), target(db)))  # another destination: fine
    account(db, add_user(db), dest)  # another user on the same destination: fine


@pytest.mark.req("EF-USR-012")
def test_a_remote_account_belongs_to_one_member_per_destination(db: sqlite3.Connection) -> None:
    dest = destination(db, shelf(db), target(db))
    account(db, add_user(db), dest, remote_user="same")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        account(db, add_user(db), dest, remote_user="same")


@pytest.mark.req("EF-USR-012")
def test_destination_account_needs_a_real_user_and_destination_and_a_name(
    db: sqlite3.Connection,
) -> None:
    dest = destination(db, shelf(db), target(db))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        account(db, new_ulid(), dest)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        account(db, add_user(db), new_ulid())
    with pytest.raises(sqlite3.IntegrityError, match="ck_destination_account_remote_user"):
        account(db, add_user(db), dest, remote_user="")


@pytest.mark.req("EF-USR-012", "ENF-SEC-005")
def test_a_live_account_holds_a_sealed_token_with_its_fingerprint(db: sqlite3.Connection) -> None:
    dest = destination(db, shelf(db), target(db))
    user = add_user(db)
    base = {"user_id": user, "shelf_destination_id": dest, "remote_user": "r"}
    # no token and not revoked: refused
    with pytest.raises(sqlite3.IntegrityError, match="ck_destination_account_live_has_token"):
        add_row(db, "destination_account", **base)
    # a half envelope: refused by the shared sealed-secret rule
    half = {**sealed(key(db)), "secret_nonce": None}
    with pytest.raises(sqlite3.IntegrityError, match="secret_sealed"):
        add_row(db, "destination_account", **base, **half)
    # a sealed token without its fingerprint (or the reverse): refused
    no_print = {**sealed(key(db)), "token_fingerprint": None}
    with pytest.raises(sqlite3.IntegrityError, match="ck_destination_account_fingerprint_iff"):
        add_row(db, "destination_account", **base, **no_print)
    account(db, user, dest)  # whole: accepted
    revoked = destination(db, shelf(db), target(db))
    account(db, user, revoked, revoked_at="2026-11-15 10:00:00")  # revoked: the token is erased


@pytest.mark.req("EF-USR-012")
def test_deleting_a_user_or_a_destination_drops_the_account(db: sqlite3.Connection) -> None:
    dest, other = destination(db, shelf(db), target(db)), destination(db, shelf(db), target(db))
    user = add_user(db)
    account(db, user, dest)
    account(db, user, other)
    db.execute("DELETE FROM shelf_destination WHERE id = ?", (other,))
    assert db.execute("SELECT count(*) FROM destination_account").fetchone() == (1,)
    db.execute('DELETE FROM "user" WHERE id = ?', (user,))
    assert db.execute("SELECT count(*) FROM destination_account").fetchone() == (0,)


# ------------------------------------------------------------ progress_sync_cursor


@pytest.mark.req("EF-USR-012", "EF-EXP-002")
def test_progress_cursor_is_per_account_and_goes_with_it(db: sqlite3.Connection) -> None:
    dest = destination(db, shelf(db), target(db))
    user = add_user(db)
    refused(db, "progress_sync_cursor", "FOREIGN KEY constraint failed",
            shelf_destination_id=dest, user_id=user)  # no account, no cursor  # fmt: skip
    account(db, user, dest)
    add_row(db, "progress_sync_cursor", shelf_destination_id=dest, user_id=user)  # both NULL
    refused(db, "progress_sync_cursor", "UNIQUE constraint failed", shelf_destination_id=dest,
            user_id=user, last_pull_server_ts="2026-11-15 10:00:00")  # fmt: skip
    db.execute(
        "UPDATE progress_sync_cursor SET last_pull_server_ts = '2026-11-15 10:00:00', "
        "last_push_at = '2026-11-15 10:05:00'"
    )
    db.execute("DELETE FROM destination_account")
    assert db.execute("SELECT count(*) FROM progress_sync_cursor").fetchone() == (0,)


# ------------------------------------------------------------------ document_alias


def alias_item(db: sqlite3.Connection) -> str:
    item = new_ulid()
    add_row(db, "export_item", id=item, edition_id=edition_row(db, add_work_row(db)), kind="unit")
    return item


@pytest.mark.req("ENF-QUAL-005", "EF-LEC-009")
def test_document_alias_keeps_a_hash_per_binding_and_algorithm(db: sqlite3.Connection) -> None:
    item = alias_item(db)
    dest = destination(db, shelf(db), target(db))
    add_row(db, "document_alias", export_item_id=item, file_hash=HASH, content_hash=HASH,
            shelf_destination_id=dest, kavita_md5=MD5, koreader_partial_md5=MD5)  # fmt: skip
    refused(db, "document_alias", "ck_document_alias_kavita_md5_md5_prefixed",
            export_item_id=item, file_hash="sha256:" + "ef" * 32, content_hash=HASH,
            kavita_md5="C" * 32)  # fmt: skip
    refused(db, "document_alias", "FOREIGN KEY constraint failed", export_item_id=item,
            file_hash="sha256:" + "12" * 32, content_hash=HASH,
            shelf_destination_id=new_ulid())  # fmt: skip
    # the alias outlives its binding: the old hash still resolves the file
    db.execute("DELETE FROM shelf_destination WHERE id = ?", (dest,))
    assert db.execute("SELECT shelf_destination_id FROM document_alias").fetchone() == (None,)
