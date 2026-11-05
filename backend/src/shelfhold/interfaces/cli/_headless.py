"""The channel and the error handling shared by `config`, `target`, `source` and `shelf`.

Same two channels as `admin` (L23): the socket of the running server, else the `flock` of
`/data/.lock` taken by the command itself. A secret is read from a file or from stdin, never from
an argument (`ps` shows `argv`), and is never printed.
"""

from __future__ import annotations

import argparse
import functools
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from shelfhold.config import ResolvedSettings, Settings
from shelfhold.export.domain.bindings import BindingError
from shelfhold.headless import HeadlessError, wiring
from shelfhold.i18n import t
from shelfhold.infrastructure.instance import (
    InstanceLock,
    InstanceLockHeldError,
    InstanceLockLinkError,
)
from shelfhold.infrastructure.local_socket import (
    RpcFailure,
    ServerUnavailable,
    UntrustedServer,
)
from shelfhold.infrastructure.migrate import current_revision, head_revision
from shelfhold.interfaces.cli.admin import ChannelRefusedError, _server_socket
from shelfhold.interfaces.local_cli import HeadlessCommands, SocketHeadless

STDIN = "-"
Handler = Callable[[argparse.Namespace, HeadlessCommands], int]


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


@contextmanager
def headless_channel(settings: Settings) -> Iterator[HeadlessCommands | None]:
    socket_path = _server_socket(settings)
    if socket_path is not None:
        yield SocketHeadless(socket_path)
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
            yield wiring.open_headless(settings)
    finally:
        lock.release()


def headless_command(
    handler: Handler,
) -> Callable[[argparse.Namespace, ResolvedSettings], int]:
    @functools.wraps(handler)
    def run(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
        try:
            with headless_channel(resolved.values) as commands:
                if commands is None:
                    return fail(t("admin.database_missing"))
                return handler(args, commands)
        except UntrustedServer as untrusted:
            return fail(t("admin.socket_untrusted", detail=untrusted))
        except InstanceLockLinkError as link:
            return fail(t("admin.lock_is_link", detail=link))
        except ChannelRefusedError as refused:
            return fail(t("admin.socket_refused", detail=refused))
        except (HeadlessError, BindingError) as refusal:
            return fail(t("headless.refused", code=refusal.code, detail=refusal))
        except (ServerUnavailable, RpcFailure, PermissionError) as failure:
            return fail(t("admin.socket_failed", detail=failure))

    return run


def read_secret(source: str) -> str:
    """The first line of a file, or of stdin for `-`; spaces kept, no newline."""
    if source == STDIN:
        raw = sys.stdin.readline()
    else:
        path = Path(source)
        if path.stat().st_mode & stat.S_IROTH:
            print(t("headless.secret_file_world_readable", path=path), file=sys.stderr)
        raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    return lines[0] if lines else ""
