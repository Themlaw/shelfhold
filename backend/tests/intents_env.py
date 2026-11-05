"""A migrated-enough SQLite database plus a synthetic library for the L22 tests (intents, locks,
plans). Real files, a real database, no mock of the OS or of SQLite."""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.schema import metadata, platform
from tests.fakes_clock import ManualClock
from tests.fsops_env import INSTANCE_ID, Env, make_env

NOW = datetime(2026, 11, 5, 8, 0, tzinfo=UTC)


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def prepare_database(
    path: Path, *, restore_epoch: int = 0, instance_id: str = INSTANCE_ID
) -> Engine:
    """Schema from the metadata (the migrations are tested by their own suite) and the one
    `instance` row, whose `restore_epoch` is the counter intents are compared with."""
    engine = create_db_engine(path)
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.insert(platform.instance).values(
                instance_id=instance_id, jurisdiction="FR", restore_epoch=restore_epoch
            )
        )
    return engine


def set_restore_epoch(engine: Engine, epoch: int) -> None:
    with engine.begin() as conn:
        conn.execute(sa.update(platform.instance).values(restore_epoch=epoch))


@dataclass
class IntentEnv:
    fs: Env
    engine: Engine
    clock: ManualClock


def make_intent_env(tmp: Path, *, restore_epoch: int = 0) -> IntentEnv:
    fs = make_env(tmp)
    return IntentEnv(
        fs, prepare_database(tmp / "shelfhold.db", restore_epoch=restore_epoch), ManualClock(NOW)
    )
