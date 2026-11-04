"""A real, migrated SQLite database for the catalog tests (no mocks): one template, one copy each.

Migrating costs a second; the template is built once per process and every harness copies the
file, so a Hypothesis run can afford a fresh database per example.
"""

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

import sqlalchemy as sa

from shelfhold.catalog.public import BindingRef, Catalog, EditionRef, EditionSpec
from shelfhold.config import load_settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import acquisition

T0 = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)


@cache
def _template() -> Path:
    root = Path(tempfile.mkdtemp(prefix="shelfhold-catalog-template-"))
    env = {"SHELFHOLD_DATA_DIR": str(root / "data"), "SHELFHOLD_STORE_DIR": str(root / "store")}
    settings = load_settings(env).values
    migrate(settings)
    return settings.database_path


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


@dataclass
class Harness:
    connection: sa.Connection
    catalog: Catalog

    def work(self, kind: str = "manga", title: str = "Work") -> str:
        return self.catalog.create_work(kind, title)

    def edition(
        self,
        work_id: str | None = None,
        *,
        kind: str = "manga",
        lang: str = "en",
        translation_type: str = "fan",
        media: str = "images",
        **extra: object,
    ) -> EditionRef:
        work_id = work_id or self.work(kind)
        return self.catalog.ensure_edition(
            EditionSpec(
                work_id=work_id,
                lang=lang,
                translation_type=translation_type,
                media=media,
                **extra,  # type: ignore[arg-type]
            )
        )

    def binding(self, edition: EditionRef, *, active: bool = True) -> BindingRef:
        """A source binding of the Edition (acquisition owns the table; the tests write it raw)."""
        item_id, binding_id = new_ulid(), new_ulid()
        self.connection.execute(
            sa.insert(acquisition.source_item).values(
                id=item_id, plugin_id="fake", source_key="fake:1", remote_id=new_ulid()
            )
        )
        priority = self.connection.execute(
            sa.select(sa.func.count()).where(acquisition.source_binding.c.edition_id == edition.id)
        ).scalar_one()
        self.connection.execute(
            sa.insert(acquisition.source_binding).values(
                id=binding_id,
                work_id=edition.work_id,
                edition_id=edition.id,
                source_item_id=item_id,
                priority=priority + 1,
                active=active,
                disabled_reason=None if active else "manual",
            )
        )
        return BindingRef(id=binding_id, edition_id=edition.id)

    def deactivate(self, binding: BindingRef) -> None:
        self.connection.execute(
            sa.update(acquisition.source_binding)
            .where(acquisition.source_binding.c.id == binding.id)
            .values(active=False, disabled_reason="manual", priority=1000)
        )

    def rows(self, sql: str, **params: object) -> list[dict[str, object]]:
        return [dict(r) for r in self.connection.execute(sa.text(sql), params).mappings()]

    def scalar(self, sql: str, **params: object) -> object:
        return self.connection.execute(sa.text(sql), params).scalar_one()


@contextmanager
def harness() -> Iterator[Harness]:
    with tempfile.TemporaryDirectory(prefix="shelfhold-catalog-") as tmp:
        path = Path(tmp) / "shelfhold.db"
        shutil.copyfile(_template(), path)
        engine = create_db_engine(path)
        try:
            with engine.begin() as connection:
                yield Harness(connection, Catalog(connection))
        finally:
            engine.dispose()
