"""``shelfhold admin create|reset-password|sessions revoke|keys create|list|revoke|rotate``.

Host CLI (EF-USR-021, EF-USR-003, EF-USR-024, ENF-OPS-015). Two channels:

* the server runs: the command goes through its Unix socket (``/data/shelfhold.sock``), as the
  ``local_cli`` principal; the audit actor is ``local_cli:<uid>``. A refusal by the socket
  (foreign uid, peer outside the PID namespace) is reported, never worked around;
* the server is stopped: the command takes the ``flock`` of ``/data/.lock`` itself and writes to
  the database directly (audit without actor, ``uid=<uid>`` in the target).

Nothing listens on the socket while a migration holds the lock, and revoking a stolen session
must not require stopping the server: with the lock held and no socket, the identity commands
still act directly (plain SQLite writes, which SQLite serialises) and take no lock.
The password comes from a file or stdin (``--password-file -``), never from the command line.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from shelfhold.config import ResolvedSettings, Settings
from shelfhold.i18n import t
from shelfhold.identity.public import (
    AdminExistsError,
    LocalAuth,
    UnknownUserError,
    local_auth,
)
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.instance import InstanceLock, InstanceLockHeldError
from shelfhold.infrastructure.local_socket import (
    SOCKET_NAME,
    RpcFailure,
    ServerUnavailable,
    UntrustedServer,
    call,
)
from shelfhold.infrastructure.migrate import current_revision, head_revision
from shelfhold.interfaces.local_cli import IdentityCommands, SocketIdentity

STDIN = "-"


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("admin", help=t("cli.admin_help"), allow_abbrev=False)
    actions = group.add_subparsers(dest="admin_command", required=True)

    for parser, handler in (
        (
            actions.add_parser("create", help=t("cli.admin_create_help"), allow_abbrev=False),
            run_create,
        ),
        (
            actions.add_parser(
                "reset-password", help=t("cli.admin_reset_help"), allow_abbrev=False
            ),
            run_reset_password,
        ),
    ):
        parser.add_argument("--username", required=True, help=t("cli.admin_username_help"))
        parser.add_argument(
            "--password-file", required=True, help=t("cli.admin_password_file_help")
        )
        parser.set_defaults(handler=handler)

    sessions = actions.add_parser("sessions", help=t("cli.admin_sessions_help"))
    session_actions = sessions.add_subparsers(dest="sessions_command", required=True)
    revoke = session_actions.add_parser(
        "revoke", help=t("cli.admin_sessions_revoke_help"), allow_abbrev=False
    )
    target = revoke.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", help=t("cli.admin_all_help"))
    target.add_argument("--user", help=t("cli.admin_user_help"))
    target.add_argument("session_id", nargs="?", help=t("cli.admin_session_id_help"))
    revoke.set_defaults(handler=run_sessions_revoke)

    keys = actions.add_parser("keys", help=t("cli.admin_keys_help"))
    key_actions = keys.add_subparsers(dest="keys_command", required=True)
    create = key_actions.add_parser(
        "create", help=t("cli.admin_keys_create_help"), allow_abbrev=False
    )
    create.add_argument("--name", help=t("cli.admin_key_name_help"))
    create.add_argument("--read-only", action="store_true", help=t("cli.admin_key_read_only_help"))
    create.add_argument(
        "--expires-days", type=int, metavar="DAYS", help=t("cli.admin_key_expires_help")
    )
    create.set_defaults(handler=run_keys_create, configure_logging=False)
    listing = key_actions.add_parser("list", help=t("cli.admin_keys_list_help"))
    listing.add_argument("--json", action="store_true", help=t("cli.json_help"))
    listing.set_defaults(handler=run_keys_list)
    revoke_key = key_actions.add_parser("revoke", help=t("cli.admin_keys_revoke_help"))
    revoke_key.add_argument("prefix", help=t("cli.admin_key_prefix_help"))
    revoke_key.set_defaults(handler=run_keys_revoke)
    rotate = key_actions.add_parser(
        "rotate", help=t("cli.admin_keys_rotate_help"), allow_abbrev=False
    )
    rotate.add_argument("prefix", help=t("cli.admin_key_prefix_help"))
    rotate.add_argument(
        "--expires-days", type=int, metavar="DAYS", help=t("cli.admin_key_rotate_expires_help")
    )
    rotate.set_defaults(handler=run_keys_rotate, configure_logging=False)


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _auth(settings: Settings) -> LocalAuth | None:
    database = settings.database_path
    if not database.exists() or current_revision(database) != head_revision():
        return None
    return local_auth(create_db_engine(database), settings)


class ChannelRefusedError(Exception):
    """The running server's socket refused this process; the command is not run another way."""


def _server_socket(settings: Settings) -> Path | None:
    """The socket of a running server, or None (absent, or left behind by a dead one)."""
    path = settings.data_dir / SOCKET_NAME
    try:
        call(path, "ping", {}, timeout_s=5.0)
    except ServerUnavailable:
        return None
    except RpcFailure as failure:
        raise ChannelRefusedError(failure.message) from failure
    return path


@contextmanager
def _identity(settings: Settings) -> Iterator[IdentityCommands | None]:
    """The channel of this command: the socket of a running server, else the database under the
    `flock` (ENF-OPS-015). None: no database at the current schema."""
    socket_path = _server_socket(settings)
    if socket_path is not None:
        yield SocketIdentity(socket_path)
        return
    auth = _auth(settings)
    if auth is None:
        yield None
        return
    lock = InstanceLock(settings.data_dir)
    # held by an instance (or a migration) without a socket: a plain SQLite write, see above
    with contextlib.suppress(InstanceLockHeldError):
        lock.acquire()
    try:
        yield auth
    finally:
        lock.release()


def _command(
    handler: Callable[[argparse.Namespace, IdentityCommands], int],
) -> Callable[[argparse.Namespace, ResolvedSettings], int]:
    @functools.wraps(handler)
    def run(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
        try:
            with _identity(resolved.values) as auth:
                if auth is None:
                    return _fail(t("admin.database_missing"))
                return handler(args, auth)
        except UntrustedServer as untrusted:
            return _fail(t("admin.socket_untrusted", detail=untrusted))
        except ChannelRefusedError as refused:
            return _fail(t("admin.socket_refused", detail=refused))
        except (ServerUnavailable, RpcFailure, PermissionError) as failure:
            return _fail(t("admin.socket_failed", detail=failure))

    return run


def _read_password(source: str) -> str:
    raw = sys.stdin.readline() if source == STDIN else Path(source).read_text(encoding="utf-8")
    lines = raw.splitlines()
    return lines[0] if lines else ""  # the first line, spaces kept; no newline


def _password_error(error: ValueError) -> str:
    reason = str(error)
    if reason == "invalid_username":
        return t("admin.invalid_username")
    if reason == "password_too_short":
        return t("admin.password_rejected", reason=t("admin.password_too_short"))
    if reason == "password_too_long":
        return t("admin.password_rejected", reason=t("admin.password_too_long"))
    return t("admin.password_rejected", reason=reason)


def _with_password(args: argparse.Namespace) -> tuple[str | None, int]:
    try:
        return _read_password(args.password_file), 0
    except (OSError, UnicodeDecodeError) as exc:
        return None, _fail(t("admin.password_unreadable", detail=exc))


@_command
def run_create(args: argparse.Namespace, auth: IdentityCommands) -> int:
    password, status = _with_password(args)
    if password is None:
        return status
    try:
        auth.create_admin(args.username, password)
    except AdminExistsError:
        return _fail(t("admin.exists"))
    except ValueError as exc:
        return _fail(_password_error(exc))
    print(t("admin.created", username=args.username.strip()))
    return 0


@_command
def run_reset_password(args: argparse.Namespace, auth: IdentityCommands) -> int:
    password, status = _with_password(args)
    if password is None:
        return status
    try:
        closed = auth.reset_password(args.username, password)
    except UnknownUserError:
        return _fail(t("admin.unknown_user", username=args.username))
    except ValueError as exc:
        return _fail(_password_error(exc))
    print(t("admin.password_reset", count=closed))
    return 0


@_command
def run_sessions_revoke(args: argparse.Namespace, auth: IdentityCommands) -> int:
    try:
        closed = auth.revoke_sessions(username=args.user, session_id=args.session_id)
    except UnknownUserError:
        return _fail(t("admin.unknown_user", username=args.user))
    print(t("admin.sessions_revoked", count=closed))
    return 0


@_command
def run_keys_create(args: argparse.Namespace, auth: IdentityCommands) -> int:
    try:
        created = auth.create_api_key(
            name=args.name, read_only=args.read_only, expires_days=args.expires_days
        )
    except UnknownUserError:
        return _fail(t("admin.unknown_user", username="admin"))
    except ValueError:
        return _fail(t("cli.admin_key_expires_help"))
    print(t("admin.key_created", prefix=created.prefix), file=sys.stderr)
    print(created.value)  # stdout carries the secret alone, once
    if created.expires_at is not None:
        print(t("admin.key_expires", date=created.expires_at.isoformat()), file=sys.stderr)
    else:
        print(t("admin.key_never_expires"), file=sys.stderr)
    return 0


@_command
def run_keys_list(args: argparse.Namespace, auth: IdentityCommands) -> int:
    keys = auth.api_keys()  # once: through the socket, each call is a round trip
    rows: list[dict[str, object]] = [
        {
            "prefix": key.prefix,
            "name": key.name,
            "scopes": list(key.scopes),
            "read_only": key.read_only,
            "created_at": key.created_at.isoformat(),
            "expires_at": key.expires_at.isoformat() if key.expires_at else None,
            "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
            "revoked": key.revoked_at is not None,
        }
        for key in keys
    ]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(t("admin.no_keys"))
    for key in keys:
        expiry = key.expires_at.isoformat() if key.expires_at else "-"
        state = "revoked" if key.revoked_at is not None else expiry
        print(
            t(
                "admin.key_line",
                prefix=key.prefix,
                name=key.name or "-",
                scopes=",".join(key.scopes),
                state=state,
            )
        )
    return 0


@_command
def run_keys_rotate(args: argparse.Namespace, auth: IdentityCommands) -> int:
    try:
        created = auth.rotate_api_key(args.prefix, expires_days=args.expires_days)
    except ValueError:
        return _fail(t("cli.admin_key_expires_help"))
    except RuntimeError:
        return _fail(t("admin.key_rotation_failed"))
    if created is None:
        return _fail(t("admin.key_unknown", prefix=args.prefix))
    print(t("admin.key_rotated", old=args.prefix, prefix=created.prefix), file=sys.stderr)
    print(created.value)  # stdout carries the secret alone, once
    if created.expires_at is not None:
        print(t("admin.key_expires", date=created.expires_at.isoformat()), file=sys.stderr)
    else:
        print(t("admin.key_never_expires"), file=sys.stderr)
    return 0


@_command
def run_keys_revoke(args: argparse.Namespace, auth: IdentityCommands) -> int:
    if not auth.revoke_api_key(args.prefix):
        return _fail(t("admin.key_unknown", prefix=args.prefix))
    print(t("admin.key_revoked", prefix=args.prefix))
    return 0
