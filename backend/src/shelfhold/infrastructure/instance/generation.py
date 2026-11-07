"""The generation counter of a physical root (I39, EF-EXP-029, EF-INT-026).

A write session moves the counter **once**, whatever it writes: the database commits the new
value first, the sentinel of the root is rewritten after. A crash between the two leaves the
database one step ahead, which `judge_sentinel` reads as consistent and the next start re-stamps.
A database restored from an older backup is the other way round (the sentinel is ahead), and that
is what `restored_behind` detects: no clock is read anywhere.
"""

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.export.domain.path_map import clean_absolute
from shelfhold.infrastructure.schema import platform

__all__ = ["GenerationCounter"]


class GenerationCounter:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @property
    def engine(self) -> Engine:
        return self._engine

    def advance(self, root_path: str) -> int | None:
        """Commit `generation + 1` for the root at `root_path`; None for a root the database does
        not know (nothing to compare the sentinel with)."""
        table = platform.physical_root
        with self._engine.begin() as connection:
            moved = connection.execute(
                sa.update(table)
                .where(table.c.path_entry == clean_absolute(root_path))
                .values(generation=table.c.generation + 1)
                .returning(table.c.generation)
            ).scalar()
        return None if moved is None else int(moved)

    def current(self, root_path: str) -> int | None:
        table = platform.physical_root
        with self._engine.connect() as connection:
            value = connection.execute(
                sa.select(table.c.generation).where(table.c.path_entry == clean_absolute(root_path))
            ).scalar()
        return None if value is None else int(value)
