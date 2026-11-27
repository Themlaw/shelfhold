"""``shelfhold admin create|reset-password|sessions revoke|keys create|list|revoke|rotate``.

Host CLI (EF-USR-021, EF-USR-003, EF-USR-024, ENF-OPS-015). Two channels:

* the server runs: the command goes through its Unix socket (``/data/shelfhold.sock``), as the
  ``local_cli`` principal; the audit actor is ``local_cli:<uid>``. A refusal by the socket
  (foreign uid, peer outside the PID namespace) is reported, never worked around;
* the server is stopped: the command takes the ``flock`` of ``/data/.lock`` itself and writes to
  the database directly (audit without actor, ``uid=<uid>`` in the target).

Revoking a stolen session or key must not require stopping the server: with the lock held and no
socket (the server only answers `/healthz`, or lost its socket), `sessions revoke` and `keys
revoke` alone still act directly, without the lock (a plain SQLite write). Every other command
refuses, and so does a revocation while a migration runs. A link on `.lock` is always refused.
The password comes from a file or stdin (``--password-file -``), never from the command line.
"""

from __future__ import annotations

import argparse
import functools
import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from shelfhold.config import ResolvedSettings, Settings
from shelfhold.i18n import t
from shelfhold.identity.domain.roles import ProfileError
from shelfhold.identity.public import (
    DEVICE_KINDS,
    AdminExistsError,
    LocalAuth,
    UnknownUserError,
    local_auth,
)
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.instance import (
    InstanceLock,
    InstanceLockHeldError,
    InstanceLockLinkError,
    holder_is_alive,
)
from shelfhold.infrastructure.local_socket import (
    SOCKET_NAME,
    RpcFailure,
    ServerUnavailable,
    UntrustedServer,
    call,
)
from shelfhold.infrastructure.migrate import (
    LOCK_FILE_NAME as MIGRATE_LOCK_NAME,
)
from shelfhold.infrastructure.migrate import (
    MigrationStateFile,
    current_revision,
    head_revision,
)
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

    acknowledge = actions.add_parser(
        "acknowledge-passkeys", help=t("cli.admin_acknowledge_passkeys_help"), allow_abbrev=False
    )
    acknowledge.add_argument(
        "--through", required=True, help=t("cli.admin_acknowledge_through_help")
    )
    acknowledge.set_defaults(handler=run_acknowledge_passkeys)
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
    create.add_argument(
        "--scope",
        choices=("read", "request", "admin", "metrics"),
        help=t("cli.admin_key_scope_help"),
    )
    create.add_argument("--subject", metavar="USERNAME", help=t("cli.admin_key_subject_help"))
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

    devices = actions.add_parser("device-tokens", help=t("cli.admin_devices_help"))
    device_actions = devices.add_subparsers(dest="device_tokens_command", required=True)
    issue = device_actions.add_parser(
        "create", help=t("cli.admin_devices_create_help"), allow_abbrev=False
    )
    issue.add_argument("--profile", required=True, help=t("cli.admin_device_profile_help"))
    issue.add_argument("--name", help=t("cli.admin_device_name_help"))
    issue.add_argument(
        "--expires-days", type=int, metavar="DAYS", help=t("cli.admin_device_expires_help")
    )
    issue.add_argument("--kind", choices=DEVICE_KINDS, help=t("cli.admin_device_kind_help"))
    issue.set_defaults(handler=run_device_tokens_create, configure_logging=False)
    device_list = device_actions.add_parser("list", help=t("cli.admin_devices_list_help"))
    device_list.add_argument("--json", action="store_true", help=t("cli.json_help"))
    device_list.set_defaults(handler=run_device_tokens_list)
    device_revoke = device_actions.add_parser("revoke", help=t("cli.admin_devices_revoke_help"))
    device_revoke.add_argument("public_id", help=t("cli.admin_device_id_help"))
    device_revoke.set_defaults(handler=run_device_tokens_revoke)
    device_rotate = device_actions.add_parser(
        "rotate", help=t("cli.admin_devices_rotate_help"), allow_abbrev=False
    )
    device_rotate.add_argument("device_id", help=t("cli.admin_device_registry_id_help"))
    device_rotate.add_argument(
        "--expires-days", type=int, metavar="DAYS", help=t("cli.admin_device_expires_help")
    )
    device_rotate.set_defaults(handler=run_device_tokens_rotate, configure_logging=False)
    device_remove = device_actions.add_parser("remove", help=t("cli.admin_devices_remove_help"))
    device_remove.add_argument("device_id", help=t("cli.admin_device_registry_id_help"))
    device_remove.set_defaults(handler=run_device_remove)


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
def _identity(settings: Settings, *, lockless: bool) -> Iterator[IdentityCommands | None]:
    """The channel of this command: the socket of a running server, else the database under the
    `flock` (ENF-OPS-015). None: no database at the current schema.

    `lockless`: a revocation may act without the lock when an instance holds it but has no
    socket, so that a stolen session or key can be closed without stopping the server. Not while
    a migration runs, and never for another command. A link on `.lock` is always a refusal."""
    socket_path = _server_socket(settings)
    if socket_path is not None:
        yield SocketIdentity(socket_path)
        return
    lock = InstanceLock(settings.data_dir)
    held = False
    try:
        lock.acquire()
        held = True
    except InstanceLockLinkError:
        raise
    except InstanceLockHeldError as error:
        if not lockless:
            raise ChannelRefusedError(t("admin.instance_without_socket")) from error
        if MigrationStateFile(settings.data_dir).read() is not None or holder_is_alive(
            settings.data_dir / MIGRATE_LOCK_NAME
        ):
            raise ChannelRefusedError(t("admin.migration_running")) from error
    try:
        auth = _auth(settings)  # the revision is read after the lock attempt, never before
        yield auth
    finally:
        if held:
            lock.release()


def _channel(
    lockless: bool,
) -> Callable[
    [Callable[[argparse.Namespace, IdentityCommands], int]],
    Callable[[argparse.Namespace, ResolvedSettings], int],
]:
    def decorate(
        handler: Callable[[argparse.Namespace, IdentityCommands], int],
    ) -> Callable[[argparse.Namespace, ResolvedSettings], int]:
        @functools.wraps(handler)
        def run(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
            try:
                with _identity(resolved.values, lockless=lockless) as auth:
                    if auth is None:
                        return _fail(t("admin.database_missing"))
                    return handler(args, auth)
            except UntrustedServer as untrusted:
                return _fail(t("admin.socket_untrusted", detail=untrusted))
            except InstanceLockLinkError as link:
                return _fail(t("admin.lock_is_link", detail=link))
            except ChannelRefusedError as refused:
                return _fail(t("admin.socket_refused", detail=refused))
            except (ServerUnavailable, RpcFailure, PermissionError) as failure:
                return _fail(t("admin.socket_failed", detail=failure))

        return run

    return decorate


_command = _channel(lockless=False)
_revocation = _channel(lockless=True)  # sessions revoke, keys revoke: the emergency exits


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
def run_acknowledge_passkeys(args: argparse.Namespace, auth: IdentityCommands) -> int:
    try:
        count = auth.acknowledge_passkeys(args.through)
    except ValueError:
        return _fail(t("admin.passkeys_unknown_reference", through=args.through))
    print(t("admin.passkeys_acknowledged", count=count))
    return 0


@_revocation
def run_sessions_revoke(args: argparse.Namespace, auth: IdentityCommands) -> int:
    try:
        closed = auth.revoke_sessions(username=args.user, session_id=args.session_id)
    except UnknownUserError:
        return _fail(t("admin.unknown_user", username=args.user))
    print(t("admin.sessions_revoked", count=closed))
    return 0


@_command
def run_keys_create(args: argparse.Namespace, auth: IdentityCommands) -> int:
    if args.scope == "request" and not args.subject:
        return _fail(t("admin.key_subject_required"))  # a request key acts as someone: name her
    try:
        created = auth.create_api_key(
            name=args.name, read_only=args.read_only, expires_days=args.expires_days,
            scope=args.scope, subject_username=args.subject,
        )  # fmt: skip
    except UnknownUserError:
        return _fail(t("admin.unknown_user", username=args.subject or "admin"))
    except ValueError as error:
        if str(error) == "key_scope_read_only_conflict":
            conflict = "errors.key_scope_read_only_conflict"
            return _fail(f"{t(conflict + '.title')} {t(conflict + '.action')}")
        if str(error) in {"invalid_scope", "scope_requires_admin", "subject_not_allowed"}:
            return _fail(t("admin.key_scope_refused"))
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


@_revocation
def run_keys_revoke(args: argparse.Namespace, auth: IdentityCommands) -> int:
    if not auth.revoke_api_key(args.prefix):
        return _fail(t("admin.key_unknown", prefix=args.prefix))
    print(t("admin.key_revoked", prefix=args.prefix))
    return 0


@_command
def run_device_tokens_create(args: argparse.Namespace, auth: IdentityCommands) -> int:
    try:
        created = auth.create_device_token(
            subject_username=args.profile, name=args.name, expires_days=args.expires_days,
            kind=args.kind,
        )  # fmt: skip
    except UnknownUserError:
        return _fail(t("admin.unknown_user", username=args.profile))
    except ProfileError:  # the subject is an administrator
        return _fail(t("admin.device_admin_subject"))
    except ValueError:
        return _fail(t("cli.admin_device_expires_help"))
    print(
        t("admin.device_created", profile=args.profile, public_id=created.public_id),
        file=sys.stderr,
    )
    print(created.secret)  # stdout carries the secret alone, once
    print(t("admin.device_hint"), file=sys.stderr)
    if created.expires_at is not None:
        print(t("admin.device_expires", date=created.expires_at.isoformat()), file=sys.stderr)
    else:
        print(t("admin.device_never_expires"), file=sys.stderr)
    return 0


@_command
def run_device_tokens_list(args: argparse.Namespace, auth: IdentityCommands) -> int:
    tokens = auth.device_tokens()  # once: through the socket, each call is a round trip
    rows: list[dict[str, object]] = [
        {
            "id": token.public_id,
            "device_id": token.device_id,
            "profile": token.profile,
            "name": token.name,
            "created_at": token.created_at.isoformat(),
            "expires_at": token.expires_at.isoformat() if token.expires_at else None,
            "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
            "revoked": token.revoked_at is not None,
        }
        for token in tokens
    ]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(t("admin.no_devices"))
    for token in tokens:
        expiry = token.expires_at.isoformat() if token.expires_at else "-"
        state = "revoked" if token.revoked_at is not None else expiry
        print(
            t(
                "admin.device_line",
                device_id=token.device_id or "-",
                public_id=token.public_id,
                profile=token.profile,
                name=token.name or "-",
                state=state,
            )
        )
    return 0


@_revocation
def run_device_tokens_revoke(args: argparse.Namespace, auth: IdentityCommands) -> int:
    if not auth.revoke_device_token(args.public_id):
        return _fail(t("admin.device_unknown", public_id=args.public_id))
    print(t("admin.device_revoked", public_id=args.public_id))
    return 0


@_command
def run_device_tokens_rotate(args: argparse.Namespace, auth: IdentityCommands) -> int:
    """A new secret on the same device: the old one is refused at once, the identifier stays."""
    try:
        rotated = auth.rotate_device(args.device_id, expires_days=args.expires_days)
    except ProfileError as error:
        if error.code == "device_rotated_meanwhile":  # another rotation won: nothing was shown
            return _fail(t("admin.device_rotated_meanwhile"))
        return _fail(t("admin.device_expiry_required"))  # past its expiry: ask for a new one
    except ValueError:
        return _fail(t("cli.admin_device_expires_help"))
    if rotated is None:
        return _fail(t("admin.device_registry_unknown", device_id=args.device_id))
    print(t("admin.device_rotated", public_id=rotated.public_id), file=sys.stderr)
    print(rotated.secret)  # stdout carries the secret alone, once
    print(t("admin.device_hint"), file=sys.stderr)
    return 0


@_revocation
def run_device_remove(args: argparse.Namespace, auth: IdentityCommands) -> int:
    if not auth.retire_device(args.device_id):
        return _fail(t("admin.device_registry_unknown", device_id=args.device_id))
    print(t("admin.device_removed", device_id=args.device_id))
    return 0
