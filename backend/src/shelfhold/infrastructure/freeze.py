"""The restore freeze, in one place (EF-INT-026, M1b L02, D11).

A database older than the disks freezes the instance until `reconcile --adopt-after-restore`.
Two things say so, and they are two states of one type:

- the persisted one, `instance.restore_generation` > 0, which survives a restart;
- the process one, set when this process finds the freeze (or is told to hold).

Three points apply it and none keeps its own copy: the write sessions (`Fsops`), the start of a
task (`Scheduler`), the services (`Repatriator.frozen`, `LoadedRoots`). This module is the only
one that reads or writes the column (an architecture test holds it to that).

The shape takes the per-root freezes of L25: `roots` names the roots frozen alone, while the
instance level freezes them all.
"""

from __future__ import annotations

from collections.abc import Iterable

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from shelfhold.infrastructure.schema import platform


class FreezeState:
    __slots__ = ("_generation", "_process", "_roots")

    def __init__(
        self, *, process: bool = False, generation: int = 0, roots: Iterable[str] = ()
    ) -> None:
        self._process = process
        self._generation = max(int(generation), 0)
        self._roots = frozenset(roots)

    # -- the database ------------------------------------------------------------------------

    @staticmethod
    def _stored(connection: Connection) -> int:
        value = connection.execute(sa.select(platform.instance.c.restore_generation)).scalar()
        return int(value or 0)

    @classmethod
    def read(cls, connection: Connection) -> FreezeState:
        """The state the database holds now (no instance row: not frozen)."""
        return cls(generation=cls._stored(connection))

    def frozen_in(self, connection: Connection) -> bool:
        """This state, or the one the database holds now: a freeze set by another writer counts.
        Nothing is cached: the persisted level is the truth."""
        return self.frozen or self._stored(connection) > 0

    def persist(self, connection: Connection, generation: int) -> None:
        """The freeze survives a restart (until the adoption); this state follows."""
        value = max(int(generation), 1)
        connection.execute(sa.update(platform.instance).values(restore_generation=value))
        self._generation = value

    def lift(self, connection: Connection) -> None:
        """The end of the freeze, written by the adoption alone."""
        connection.execute(sa.update(platform.instance).values(restore_generation=0))
        self._generation = 0
        self._process = False

    # -- this process ------------------------------------------------------------------------

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def frozen(self) -> bool:
        """The whole instance: the persisted level or this process's."""
        return self._process or self._generation > 0

    def frozen_for(self, root_id: str) -> bool:
        return self.frozen or root_id in self._roots

    def freeze(self) -> None:
        self._process = True

    def thaw(self) -> None:
        """Let go in this process only: the database keeps its word (see `lift`)."""
        self._process = False
        self._generation = 0

    def fork(self) -> FreezeState:
        """Each writer owns its copy: thawing one never thaws another."""
        return FreezeState(process=self._process, generation=self._generation, roots=self._roots)
