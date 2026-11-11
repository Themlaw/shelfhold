"""The locks the production writers are given, for the tests that build them: explicit, never a
default (a writer without its lock is refused by construction, EF-BIB-052)."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from sqlalchemy import Engine

from shelfhold.infrastructure.clock import SystemClock
from shelfhold.library.public import WorkHolder
from shelfhold.work_locks import holder_over, index_holder_over


def work_lock(engine: Engine) -> WorkHolder:
    return holder_over(engine, SystemClock())


def index_lock(engine: Engine) -> Callable[[], AbstractContextManager[Any]]:
    return index_holder_over(engine, SystemClock())
