"""The channel and the error handling of the operating commands (EF-INT-023).

Same two channels as `admin` and `config` (L23): the socket of the running server, else the
`flock` of `/data/.lock` taken by the command itself. A secret is read from a file or from stdin,
never from an argument.
"""

from __future__ import annotations

import argparse
import functools
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from shelfhold.config import ResolvedSettings, Settings
from shelfhold.export.domain.bindings import BindingError
from shelfhold.headless import HeadlessError
from shelfhold.i18n import t
from shelfhold.infrastructure.instance import (
    InstanceLock,
    InstanceLockHeldError,
    InstanceLockLinkError,
)
from shelfhold.infrastructure.local_socket import RpcFailure, ServerUnavailable, UntrustedServer
from shelfhold.infrastructure.migrate import current_revision, head_revision
from shelfhold.interfaces.cli.admin import ChannelRefusedError, _server_socket
from shelfhold.interfaces.local_cli import LocalChannel, OpsChannel, OpsRefusal, SocketChannel
from shelfhold.ops.wiring import open_ops

__all__ = ["fail", "open_ops", "ops_command"]


def fail(message: str, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


Handler = Callable[[argparse.Namespace, OpsChannel], int]


@contextmanager
def ops_channel(settings: Settings) -> Iterator[OpsChannel | None]:
    socket_path = _server_socket(settings)
    if socket_path is not None:
        yield SocketChannel(socket_path)
        return
    lock = InstanceLock(settings.data_dir)
    try:
        lock.acquire()
    except InstanceLockHeldError as error:  # a server holds it without a socket: no other way
        raise ChannelRefusedError(t("admin.instance_without_socket")) from error
    try:
        database = settings.database_path
        if not database.exists() or current_revision(database) != head_revision():
            yield None
        else:
            yield LocalChannel(open_ops(settings))
    finally:
        lock.release()


def ops_command(handler: Handler) -> Callable[[argparse.Namespace, ResolvedSettings], int]:
    @functools.wraps(handler)
    def run(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
        try:
            with ops_channel(resolved.values) as channel:
                if channel is None:
                    return fail(t("admin.database_missing"))
                return handler(args, channel)
        except UntrustedServer as untrusted:
            return fail(t("admin.socket_untrusted", detail=untrusted))
        except InstanceLockLinkError as link:
            return fail(t("admin.lock_is_link", detail=link))
        except ChannelRefusedError as refused:
            return fail(t("admin.socket_refused", detail=refused))
        except OpsRefusal as refusal:
            return fail(t("ops.refused", code=refusal.code, detail=refusal))
        except (HeadlessError, BindingError) as refusal:
            return fail(t("headless.refused", code=refusal.code, detail=refusal))
        except (ServerUnavailable, RpcFailure, PermissionError) as failure:
            return fail(t("admin.socket_failed", detail=failure))

    return run


def say(message: str, **values: Any) -> None:
    print(t(message, **values), file=sys.stdout)
