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
from shelfhold.export.targets.kavita.account_session import KavitaAccountSession
from shelfhold.export.targets.kavita.dto import HIDDEN_DIR_PATTERNS
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.export_env import ExportEnv, export_env
from tests.fakes.kavita import API_KEY, BASE_URL, FakeKavita, build_fetcher
from tests.kavita_export_env import Wired, wire
from tests.purge_env import ADMIN, KEY, READER, STALE

CHILD = Principal(
    "01J00000000000000000000KID", "kid", "child", Credential.SESSION,
    session_id="01J0000000000000000000SES1",
)  # fmt: skip
PRE_ADOPTION = {"library_type": "Manga", "exclude_patterns": ["*.tmp"]}  # as adopt.py writes it
OTHERS = (KEY, STALE, READER, CHILD)  # nobody of these may remove or detach anything


def fresh_admin(env: ExportEnv) -> Principal:
    return Principal(
        "01J00000000000000000000ADM", "admin", "admin", Credential.SESSION,
        session_id="01J0000000000000000000SES2", auth_time=env.clock.now(),
    )  # fmt: skip


@dataclass
class Revoker:
    """What the Kavita account's door does, against the simulated server: it records the order of
    the deeds, names the key by its identifier and deletes that identifier and no other."""

    fake: FakeKavita | None = None
    calls: list[tuple[str, str | None]] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)  # the identifiers it was asked to revoke
    answer: bool = True
    raises: BaseException | None = None
    seen_before: Callable[[], Any] | None = None  # looked at when the key is asked to go

    def key_id(self, target_id: str, *, credential: str | None) -> str | None:
        if self.fake is None:
            return "1"
        row = next((k for k in self.fake.auth_keys if k["key"] == API_KEY), None)
        return None if row is None else str(row["id"])

    def revoke_id(self, target_id: str, *, credential: str | None, key_id: str) -> str:
        if self.seen_before is not None:
            self.seen_before()
        if self.raises is not None:
            raise self.raises
        self.calls.append((target_id, credential))
        self.ids.append(key_id)
        if not self.answer:
            return "failed"
        if self.fake is not None:
            row = next((k for k in self.fake.auth_keys if str(k["id"]) == key_id), None)
            if row is not None and row["key"] != API_KEY:  # the sealed value is the dedicated key's
                return "reused"  # the identifier now names another key: not ours
            # gone by this identifier only; already gone is still gone
            self.fake.auth_keys = [k for k in self.fake.auth_keys if str(k["id"]) != key_id]
        return "revoked"

    def revoke(self, target_id: str, *, credential: str | None) -> bool:
        key = self.key_id(target_id, credential=credential)
        return key is not None and (
            self.revoke_id(target_id, credential=credential, key_id=key) == "revoked"
        )


@dataclass
class DetachEnv:
    env: ExportEnv
    wired: Wired
    journal: IntentJournal
    remover: RemoveDestination
    detacher: FullDetacher
    revoker: Revoker
    library_roots: set[str]

    @property
    def root(self) -> Path:
        """The physical root the destination lives in (its `.shelfhold` is here)."""
        return self.env.lib.library

    def account_session(self, stored_fingerprint: str | None = None) -> KavitaAccountSession:
        """The admin account's login on the simulated Kavita (what `--key-gone` settles through)."""
        username, password = self.wired.fake.account
        return KavitaAccountSession(
            build_fetcher(self.wired.fake), BASE_URL, username, password,
            stored_fingerprint=stored_fingerprint,
        )  # fmt: skip

    def instance_fingerprint(self) -> str | None:
        """What the target was linked to: the fingerprint of the simulated Kavita as it is now."""
        import asyncio

        session = self.account_session()  # built outside the loop: the fetcher opens its own

        async def read() -> str | None:
            await session._headers()  # the login
            return await session.fingerprint()

        return asyncio.run(read())

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
            with write_transaction(env.engine) as conn:
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
