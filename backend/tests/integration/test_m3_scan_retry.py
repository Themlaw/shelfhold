"""A Kavita scan that could not be asked is written down and asked again, never swallowed
(EF-BIB-021, M3 closure review, major 3): the lost scans of a withdrawal or a detachment, and
the retry the export run makes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from shelfhold.export.application.pending_scans import (
    load_pending_scans,
    remember_scans,
    replace_pending_scans,
)
from shelfhold.export.targets.kavita.errors import TargetUnreachable
from shelfhold.infrastructure.db import write_transaction
from shelfhold.ops.library import LibraryOps, ScanUnavailableError
from tests.export_env import export_env

pytestmark = pytest.mark.req("EF-BIB-021")


class _Client:
    def __init__(self, up: bool) -> None:
        self.up = up
        self.scanned: list[str] = []

    async def scan_folder(self, path: str) -> None:
        if not self.up:
            raise TargetUnreachable("Kavita is down")
        self.scanned.append(path)


def _ops(env: object, client: _Client) -> LibraryOps:
    runtime = SimpleNamespace(engine=env.engine, kavita_client=lambda _target: client)  # type: ignore[attr-defined]
    return LibraryOps(runtime)  # type: ignore[arg-type]


def test_an_unreachable_kavita_is_not_swallowed_and_is_asked_again_later() -> None:
    with export_env() as env:
        client = _Client(up=False)
        ops = _ops(env, client)
        with pytest.raises(ScanUnavailableError):
            ops._scan_everywhere(["/data/Kavita/Dr Stone"])
        remember_scans(env.engine, ["/data/Kavita/Dr Stone", "/data/Kavita/Dr Stone"])
        with env.engine.connect() as connection:
            assert load_pending_scans(connection) == ["/data/Kavita/Dr Stone"]  # once, not twice

        assert ops.retry_pending_scans() == {"removed": 0, "waiting": 1}  # still down: kept
        client.up = True
        assert ops.retry_pending_scans() == {"removed": 0, "waiting": 0}
        assert client.scanned == ["/data/Kavita/Dr Stone"]
        with env.engine.connect() as connection:
            assert load_pending_scans(connection) == []  # asked, so forgotten
        assert ops.retry_pending_scans() == {"removed": 0, "waiting": 0}  # nothing waits


def test_the_pending_scans_can_be_replaced_and_emptied() -> None:
    with export_env() as env:
        with write_transaction(env.engine) as connection:
            replace_pending_scans(connection, ["a", "b"])
        with env.engine.connect() as connection:
            assert load_pending_scans(connection) == ["a", "b"]
        with write_transaction(env.engine) as connection:
            replace_pending_scans(connection, [])
        with env.engine.connect() as connection:
            assert load_pending_scans(connection) == []


def test_withdraw_apply_keeps_the_scan_when_kavita_is_down_and_the_retry_asks_it() -> None:
    """The files left the library of the children but Kavita could not be asked: the result says
    the scan is pending, the folder to scan is written down, the next export run asks for it."""
    from contextlib import contextmanager
    from typing import Any

    import shelfhold.ops.library as library
    from shelfhold.identity.public import Credential, Principal

    class Service:
        def __init__(self, *_a: Any, request_scan: Any, **_k: Any) -> None:
            self._scan = request_scan

        def apply(self, **_k: Any) -> Any:
            self._scan("/data/Kavita/Dr Stone")
            return SimpleNamespace(withdrawn=("e1",), files=2, pending=())

    with export_env() as env:
        client = _Client(up=False)
        write = SimpleNamespace(fsops=None, loaded=SimpleNamespace(locate=None))

        @contextmanager
        def write_context() -> Any:
            yield write

        runtime = SimpleNamespace(
            engine=env.engine, kavita_client=lambda _target: client, write_context=write_context,
            clock=env.clock, settings=SimpleNamespace(store_dir="/store", fresh_auth_s=900),
        )  # fmt: skip
        ops = LibraryOps(runtime)  # type: ignore[arg-type]
        admin = Principal("01J00000000000000000000ADM", "admin", "admin", Credential.LOCAL_CLI)
        original = library.WithdrawalService
        library.WithdrawalService = Service  # type: ignore[misc,assignment]
        try:
            done = ops.withdraw_apply(if_match="v", principal=admin)
        finally:
            library.WithdrawalService = original  # type: ignore[misc]
        assert done["withdrawn"] == ["e1"] and done["scan_pending"] is True
        with env.engine.connect() as connection:
            assert load_pending_scans(connection) == ["/data/Kavita/Dr Stone"]
        client.up = True
        ops.retry_pending_scans()
        assert client.scanned == ["/data/Kavita/Dr Stone"]
        with env.engine.connect() as connection:
            assert load_pending_scans(connection) == []
