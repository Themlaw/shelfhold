"""Composition of the local authentication over one SQLite engine."""

from sqlalchemy import Engine

from shelfhold.config import Settings
from shelfhold.identity.adapters.passwords import Argon2Hasher
from shelfhold.identity.adapters.store import SqlIdentityStore
from shelfhold.identity.application.service import LocalAuth
from shelfhold.infrastructure.clock import Clock, SystemClock


def local_auth(engine: Engine, settings: Settings, clock: Clock | None = None) -> LocalAuth:
    return LocalAuth(
        SqlIdentityStore(engine),
        Argon2Hasher(),
        clock or SystemClock(),
        hardened=settings.auth_hardened,
        fresh_auth_s=settings.fresh_auth_s,
    )
