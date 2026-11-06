"""No thread closes a descriptor it does not own at start-up (seen as `ENOTDIR` on
`/proc/self/fd/22/s`: a number closed twice, then reused by another file, is a wrong-file write)."""

import os
import threading
import traceback

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import migrate
from shelfhold.main import create_app

pytestmark = pytest.mark.req("EF-USR-024")


def test_start_up_closes_only_its_own_descriptors(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open, real_dup, real_close = os.open, os.dup, os.close
    owners: dict[int, str] = {}
    faults: list[str] = []

    def note(fd: int) -> int:
        owners[fd] = threading.current_thread().name
        return fd

    def tracked_open(*args: object, **kwargs: object) -> int:
        return note(real_open(*args, **kwargs))  # type: ignore[arg-type]

    def tracked_dup(fd: int) -> int:
        return note(real_dup(fd))

    def tracked_close(fd: int) -> None:
        owner = owners.pop(fd, None)
        here = threading.current_thread().name
        try:
            real_close(fd)
        except OSError as error:
            faults.append(f"close({fd}) by {here}: {error}\n{''.join(traceback.format_stack())}")
            raise
        if owner is not None and owner != here:
            faults.append(f"fd {fd} opened by {owner}, closed by {here}")

    migrate(settings)
    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "dup", tracked_dup)
    monkeypatch.setattr(os, "close", tracked_close)
    for _ in range(8):
        with TestClient(create_app(settings)):
            pass
    monkeypatch.undo()
    assert faults == []
