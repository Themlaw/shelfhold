"""The removal of a destination and the full detachment (M3 L30, EF-EXP-031): a shelf exported
through the real pipeline, the simulated Kavita, and the two services over a real database and a
real tree. Nothing is mocked but the three outside doors (the key revoker, the Kavita client's
network, the clock)."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from shelfhold.export.application.detach import FullDetacher
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.remove_destination import RemoveDestination
from shelfhold.export.targets.kavita.dto import HIDDEN_DIR_PATTERNS
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.export_env import ExportEnv, export_env
from tests.kavita_export_env import Wired, wire
from tests.purge_env import ADMIN, KEY, READER, STALE

CHILD = Principal(
    "01J00000000000000000000KID", "kid", "child", Credential.SESSION,
    session_id="01J0000000000000000000SES1",
)  # fmt: skip
PRE_ADOPTION = {"library_type": "manga", "exclude_patterns": ["*.tmp"]}
OTHERS = (KEY, STALE, READER, CHILD)  # nobody of these may remove or detach anything


def fresh_admin(env: ExportEnv) -> Principal:
    return Principal(
        "01J00000000000000000000ADM", "admin", "admin", Credential.SESSION,
        session_id="01J0000000000000000000SES2", auth_time=env.clock.now(),
    )  # fmt: skip


@dataclass
class Revoker:
    """What the Kavita account's door does: it records the order of the deeds."""

    calls: list[tuple[str, str | None]] = field(default_factory=list)
    answer: bool = True
    raises: BaseException | None = None
    seen_before: Callable[[], Any] | None = None  # looked at when the key is asked to go

    def revoke(self, target_id: str, *, credential: str | None) -> bool:
        if self.seen_before is not None:
            self.seen_before()
        if self.raises is not None:
            raise self.raises
        self.calls.append((target_id, credential))
        return self.answer


@dataclass
class DetachEnv:
    env: ExportEnv
    wired: Wired
    journal: IntentJournal
    remover: RemoveDestination
    detacher: FullDetacher
    revoker: Revoker

    @property
    def root(self) -> Path:
        """The physical root the destination lives in (its `.shelfhold` is here)."""
        return self.env.lib.library

    def kavita_patterns(self) -> list[str]:
        return list(self.wired.fake.libraries[0]["excludePatterns"])

    def hidden(self, base: Path | None = None) -> list[str]:
        """Every Shelfhold trace in a tree: the reserved folder, the sentinel, the probes."""
        found: list[str] = []
        top = base or self.root
        for path in sorted(top.rglob("*")):
            if path.name.startswith(".shelfhold") or ".shelfhold" in path.relative_to(top).parts:
                found.append(str(path.relative_to(top)))
        return found

    def intents(self, *states: str) -> list[sa.Row[Any]]:
        from shelfhold.infrastructure.schema import coherence

        with self.env.engine.connect() as conn:
            stmt = sa.select(coherence.fs_intent)
            if states:
                stmt = stmt.where(coherence.fs_intent.c.state.in_(states))
            return list(conn.execute(stmt))

    def intent_ids(self) -> list[str]:
        """Every intention in the journal, whatever its state: a refusal adds none."""
        return sorted(str(i.id) for i in self.intents())

    def count(self, table: sa.Table) -> int:
        with self.env.engine.connect() as conn:
            return int(conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())

    def tree_digest(self) -> dict[str, tuple[int, int]]:
        """`{path: (inode, size)}` of every exported file: a detachment must not touch them."""
        return {
            str(p.relative_to(self.env.root)): (p.stat().st_ino, p.stat().st_size)
            for p in self.env.files()
        }


@contextmanager
def detach_env(*, adopted: bool = True) -> Iterator[DetachEnv]:
    from tests.detach_services import build_services

    with export_env() as env:
        wired = wire(env)
        if adopted:  # the library as an adoption left it: the hidden-folder pattern added
            wired.fake.libraries[0]["excludePatterns"] = ["*.tmp", *HIDDEN_DIR_PATTERNS]
            with env.engine.begin() as conn:
                conn.execute(
                    sa.update(export.shelf_destination).values(
                        adopted=True, pre_adoption_settings=PRE_ADOPTION
                    )
                )
        yield build_services(env, wired)


def another_id() -> str:
    return new_ulid()


__all__ = [
    "ADMIN", "CHILD", "KEY", "OTHERS", "PRE_ADOPTION", "READER", "STALE",
    "DetachEnv", "Revoker", "detach_env", "fresh_admin",
]  # fmt: skip
