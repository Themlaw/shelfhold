"""The restore freeze, in one place (EF-INT-026, M1b L02, D11).

A database older than the disks freezes the instance until `reconcile --adopt-after-restore`.
Two things say so, and they are two states of one type:

- the persisted one, `instance.restore_generation` > 0, which survives a restart;
- the process one, set when this process finds the freeze (or is told to hold).

Three points apply it and none keeps its own copy: the write sessions (`Fsops`), the start of a
task (`Scheduler`), the services (`Repatriator.frozen`, `LoadedRoots`). This module is the only
one that reads or writes the column (an architecture test holds it to that).

The per-root freezes of L25 live here too: a root whose sentinel is another instance's
(`foreign_root`), whose disk was replaced (`root_lost`), restored from before the database
(`root_behind`), or the lost `/store` (`store_lost`) is frozen alone, the instance goes on. The
state is a waiting mark (`app_setting`, key `waiting.<state>.<root id>`, no migration, the format
of `export.application.waiting_marks`); this module is the only one that decides which marks
freeze a root, and `persist_root` / `lift_root` are the only writers of those four. The instance
level freezes every root; a root level freezes its root.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Final

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import platform

WAITING_PREFIX: Final = "waiting."
ROOT_STATES: Final = ("foreign_root", "root_lost", "root_behind", "store_lost")


def root_mark_key(state: str, subject: str) -> str:
    return f"{WAITING_PREFIX}{state}.{subject}"


class FreezeState:
    __slots__ = ("_generation", "_process", "_root_states", "_roots", "_thawed")

    def __init__(
        self, *, process: bool = False, generation: int = 0, roots: Iterable[str] = ()
    ) -> None:
        self._process = process
        self._generation = max(int(generation), 0)
        self._roots = frozenset(roots)
        self._root_states: Mapping[str, str] = {r: "root_lost" for r in self._roots}
        self._thawed = False  # a deliberate local thaw (the adoption's deletion window)

    # -- the database ------------------------------------------------------------------------

    @staticmethod
    def _stored(connection: Connection) -> int:
        value = connection.execute(sa.select(platform.instance.c.restore_generation)).scalar()
        return int(value or 0)

    @staticmethod
    def stored_roots(connection: Connection) -> dict[str, str]:
        """`root id (or path) -> state` of the roots frozen alone, as the database holds them."""
        table = platform.app_setting
        found: dict[str, str] = {}
        for state in ROOT_STATES:
            prefix = root_mark_key(state, "")
            for key in connection.execute(
                sa.select(table.c.key).where(sa.func.substr(table.c.key, 1, len(prefix)) == prefix)
            ).scalars():
                found[key[len(prefix) :]] = state
        return found

    @classmethod
    def read(cls, connection: Connection) -> FreezeState:
        """The state the database holds now (no instance row: not frozen)."""
        state = cls(generation=cls._stored(connection))
        state._follow_roots(cls.stored_roots(connection))
        return state

    def _follow_roots(self, states: Mapping[str, str]) -> None:
        self._root_states = dict(states)
        self._roots = frozenset(states)

    def persist_root(
        self, connection: Connection, subject: str, state: str, *, reason: str, path: str = ""
    ) -> None:
        """Freeze one root, in the caller's transaction (one mark per root: a new state replaces
        the old one). `follow_root` once the transaction has committed."""
        if state not in ROOT_STATES:
            raise ValueError(f"{state} is not a root state")
        self.lift_root(connection, subject)
        connection.execute(
            sa.insert(platform.app_setting).values(
                key=root_mark_key(state, subject),
                value=json.dumps({"reason": reason, "path": path, "root_id": subject}),
            )
        )

    def lift_root(self, connection: Connection, subject: str) -> None:
        """Take every root mark of `subject` off (the exit of a root state), in the caller's
        transaction."""
        keys = [root_mark_key(state, subject) for state in ROOT_STATES]
        connection.execute(
            sa.delete(platform.app_setting).where(platform.app_setting.c.key.in_(keys))
        )

    def follow_root(self, subject: str, state: str | None) -> None:
        """The root marks were committed: this state follows (None: the root is free)."""
        states = dict(self._root_states)
        if state is None:
            states.pop(subject, None)
        else:
            states[subject] = state
        self._follow_roots(states)

    def state_of(self, root_id: str) -> str | None:
        """Which of the four states freezes this root alone (None: not frozen alone)."""
        return self._root_states.get(root_id)

    def frozen_in(self, connection: Connection) -> bool:
        """This state, or the one the database holds now: a freeze set by another writer counts.
        Nothing is cached: the persisted level is the truth."""
        return self.frozen or self._stored(connection) > 0

    def persist(self, connection: Connection, generation: int) -> int:
        """Write the freeze in the caller's transaction and return the value written. Nothing
        changes in memory: `follow` does that once the transaction has committed."""
        value = max(int(generation), 1)
        connection.execute(sa.update(platform.instance).values(restore_generation=value))
        return value

    def follow(self, generation: int) -> None:
        """The freeze that `persist` wrote is committed: this state follows."""
        self._generation = max(int(generation), 0)
        self._thawed = False

    def lift(self, engine: Engine) -> None:
        """The end of the freeze, written by the adoption alone. This state is thawed only after
        the commit: a commit that fails leaves it frozen, like the database."""
        with write_transaction(engine) as connection:
            connection.execute(sa.update(platform.instance).values(restore_generation=0))
        self._generation = 0
        self._process = False
        self._thawed = False

    def sync(self, connection: Connection) -> bool:
        """Read the persisted level and, unless this writer was thawed on purpose, follow it: a
        writer opened before the freeze stops writing at its next session or write."""
        stored = self._stored(connection)
        if stored > 0 and not self._thawed:
            self._generation = stored
        self._follow_roots(self.stored_roots(connection))  # the roots frozen alone: always read
        return self.frozen

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
        self._thawed = False

    def thaw(self) -> None:
        """Let go in this process only: the database keeps its word (see `lift`)."""
        self._process = False
        self._generation = 0
        self._thawed = True

    def fork(self) -> FreezeState:
        """Each writer owns its copy: thawing one never thaws another."""
        copy = FreezeState(process=self._process, generation=self._generation)
        copy._follow_roots(self._root_states)
        return copy
