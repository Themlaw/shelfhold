"""Seeds for the anti-echo rule (M2 L22, I50): an export of Shelfhold's own, as the database would
hold it after a real export, so that a file coming back from a source can be compared to it."""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

from shelfhold.export.domain.naming import DEFAULT_TEMPLATE
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, storage
from tests.integration.test_schema_catalog import add_edition, add_work
from tests.library_env import LibEnv


@dataclass(frozen=True)
class OwnExport:
    export_item_id: str
    edition_id: str
    destination_id: str


def seed_own_export(
    lib: LibEnv,
    *,
    file_hash: str | None = None,
    content_hash: str | None = None,
    alias_file_hash: str | None = None,
    alias_content_hash: str | None = None,
    kept_content_hash: str | None = None,
    kept_derived_hash: str | None = None,
) -> OwnExport:
    """One `export_item` of its own Work with the hashes given: on its `export_record` (the file
    rendered for a destination, the payload as received), in an old `document_alias`, or on the
    `content` it was made from."""
    target_id, shelf_id, destination_id = new_ulid(), new_ulid(), new_ulid()
    item_id = new_ulid()
    with lib.engine.begin() as connection:
        edition_id = add_edition(connection, add_work(connection), lang="en", media="text")
        connection.execute(
            sa.insert(export.export_target).values(
                id=target_id, kind="kavita", name="Kavita", path_map=[]
            )
        )
        connection.execute(sa.insert(export.shelf).values(id=shelf_id, name="Livres", phrase="x"))
        connection.execute(
            sa.insert(export.shelf_destination).values(
                id=destination_id, shelf_id=shelf_id, target_id=target_id, transport="tree",
                egress="foyer", root_subpath="books", template=DEFAULT_TEMPLATE.dumps(),
                template_origin=f"default@{DEFAULT_TEMPLATE.version}",
            )
        )  # fmt: skip
        content_id = None
        if kept_content_hash is not None or kept_derived_hash is not None:
            content_id = new_ulid()
            connection.execute(
                sa.insert(storage.content).values(
                    id=content_id, kind="file", media_type="application/epub+zip", format="epub",
                    retention="adopted_ref", original_sha256="sha256:" + "0" * 64,
                    content_hash=kept_content_hash, derived_hash=kept_derived_hash,
                    derived_by="xhtml-neutralizer@v1" if kept_derived_hash else None,
                    store_provenance="received", size=10,
                )
            )  # fmt: skip
        connection.execute(
            sa.insert(export.export_item).values(
                id=item_id, edition_id=edition_id, kind="unit", content_id=content_id
            )
        )
        connection.execute(
            sa.insert(export.export_record).values(
                shelf_destination_id=destination_id, export_item_id=item_id, locator_type="path",
                rel_path="Livres/Un livre.epub", file_hash=file_hash, content_hash=content_hash,
                state="written",
            )
        )  # fmt: skip
        if alias_file_hash is not None and alias_content_hash is not None:
            connection.execute(
                sa.insert(export.document_alias).values(
                    export_item_id=item_id,
                    file_hash=alias_file_hash,
                    content_hash=alias_content_hash,
                )
            )
    return OwnExport(item_id, edition_id, destination_id)
