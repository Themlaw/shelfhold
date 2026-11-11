"""The Work, Edition, Unit and Variant of a file dropped in the import folder (EF-SRC-012).

The same road as a file a source hands over (`NativeAcquirer._record_request`): a binding to a
source item of the native `import_folder` source, the Edition filed through `file_new_edition`
(the one door, so a shelf below 18+ never takes it unrated, EF-BIB-055), the Unit and Variant by
`Catalog.ingest`. The source item is the file's own hash: the same bytes dropped twice find the
same binding and the same Variant. The binding says what the import declared (`publisher` only for
a purchase the admin declared); the trust of an existing binding is never raised by a later drop.

The Unit is made only when nothing is known to refuse the file (the import's precheck runs
first), so a refused file leaves no empty Unit behind; a store that fails afterwards (a full disk)
leaves the Unit to be filled by the next scan, which finds the same binding by the file's hash.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.acquisition.application.acquire import UNKNOWN_LANGUAGE, _shelf
from shelfhold.catalog.public import BindingRef, Catalog, IncomingUnit, file_new_edition
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog
from shelfhold.library.public import ImportItem

__all__ = ["IMPORT_PLUGIN", "ImportTargets"]

IMPORT_PLUGIN = "import_folder"
# what the file is -> (kind of Work, media of the Edition, kind of Unit)
_SHAPES = {
    "cbz": ("comic", "images", "chapter"),
    "epub": ("ebook", "text", "book"),
    "pdf": ("ebook", "text", "book"),
}


class ImportTargets:
    def __init__(self, engine: Engine, clock: Clock, *, shelf_id: str | None = None) -> None:
        self._engine = engine
        self._clock = clock
        self._shelf = shelf_id

    def __call__(self, item: ImportItem) -> tuple[str, str]:
        work_kind, media, unit_kind = _SHAPES[item.kind]
        digest = item.sha256.removeprefix("sha256:")
        now = self._clock.now()
        with self._engine.connect() as connection:
            connection.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
            with connection.begin():
                binding_id, _work_id, edition_id, _created = self._binding(
                    connection, item, digest, work_kind, media, now
                )
                result = Catalog(connection).ingest(
                    IncomingUnit(
                        binding=BindingRef(binding_id, edition_id), remote_unit_id=digest,
                        raw_number=None, seen_at=now, title=item.title, kind=unit_kind,
                    )
                )  # fmt: skip
        return result.unit_id, result.variant_id

    def _binding(
        self, connection: Connection, item: ImportItem, digest: str, work_kind: str, media: str,
        now: datetime,
    ) -> tuple[str, str, str, bool]:  # fmt: skip
        source, binding = acquisition.source_item, acquisition.source_binding
        found = connection.execute(
            sa.select(binding.c.id, binding.c.work_id, binding.c.edition_id)
            .select_from(binding.join(source, source.c.id == binding.c.source_item_id))
            .where(source.c.plugin_id == IMPORT_PLUGIN, source.c.remote_id == digest)
        ).first()
        if found is not None:
            return found.id, found.work_id, found.edition_id, False
        work_id = Catalog(connection).create_work(work_kind, item.title)
        edition_id = new_ulid()
        connection.execute(
            sa.insert(catalog.edition).values(
                id=edition_id, work_id=work_id, lang=UNKNOWN_LANGUAGE,
                translation_type="official", media=media,
            )
        )  # fmt: skip
        file_new_edition(connection, edition_id, self._shelf or _shelf(connection))
        item_id = new_ulid()
        connection.execute(
            sa.insert(source).values(
                id=item_id, plugin_id=IMPORT_PLUGIN, source_key=IMPORT_PLUGIN, remote_id=digest,
                last_seen=now,
            )
        )  # fmt: skip
        binding_id = new_ulid()
        connection.execute(
            sa.insert(binding).values(
                id=binding_id, work_id=work_id, edition_id=edition_id, source_item_id=item_id,
                priority=1, active=True, content_trust=item.trust,
            )
        )  # fmt: skip
        return binding_id, work_id, edition_id, True
