"""``shelfhold profile add|list|set|remove`` (M3 L05, EF-USR-001, EF-USR-005, EF-USR-024).

The household profiles from the host: the same rules as the HTTP routes (a child always has an
age ceiling and never the right to classify, at most two admins, the last admin stays), through
the socket of a running server (principal `local_cli`) or directly under the lock of the instance
when it is stopped. The password comes from a file or from the standard input, never from the
command line.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable

from shelfhold.i18n import t
from shelfhold.identity.domain.policy import SOURCE_POLICIES
from shelfhold.identity.public import ProfileError, ProfileRecord
from shelfhold.interfaces.cli.admin import _command, _read_password
from shelfhold.interfaces.local_cli import IdentityCommands

ROLES = ("member", "child", "admin")


def _source_policy(text: str) -> tuple[str, str]:
    source, separator, policy = text.partition("=")
    if not separator or not source or policy not in SOURCE_POLICIES:
        raise argparse.ArgumentTypeError(t("profiles.source_policy_format"))
    return source, policy


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("profile", help=t("cli.profile_help"), allow_abbrev=False)
    actions = group.add_subparsers(dest="profile_command", required=True)

    add = actions.add_parser("add", help=t("cli.profile_add_help"), allow_abbrev=False)
    add.add_argument("username", help=t("cli.profile_username_help"))
    add.add_argument("--role", choices=ROLES, default="member", help=t("cli.profile_role_help"))
    add.add_argument("--max-age", type=int, help=t("cli.profile_max_age_help"))
    add.add_argument(
        "--can-rate", action="store_true", default=None, help=t("cli.profile_can_rate_help")
    )
    add.add_argument("--password-file", required=True, help=t("cli.admin_password_file_help"))
    add.set_defaults(handler=run_add)

    listing = actions.add_parser("list", help=t("cli.profile_list_help"), allow_abbrev=False)
    listing.add_argument("--json", action="store_true", help=t("cli.json_help"))
    listing.set_defaults(handler=run_list)

    change = actions.add_parser("set", help=t("cli.profile_set_help"), allow_abbrev=False)
    change.add_argument("username", help=t("cli.profile_username_help"))
    change.add_argument("--role", choices=ROLES, help=t("cli.profile_role_help"))
    change.add_argument("--max-age", type=int, help=t("cli.profile_max_age_help"))
    change.add_argument(
        "--can-rate", action=argparse.BooleanOptionalAction, default=None,
        help=t("cli.profile_can_rate_help"),
    )  # fmt: skip
    change.add_argument(
        "--source-policy", action="append", type=_source_policy, default=[],
        metavar="SOURCE=POLICY", help=t("cli.profile_source_policy_help"),
    )  # fmt: skip
    change.set_defaults(handler=run_set)

    mapping = actions.add_parser(
        "map-kavita", help=t("cli.profile_map_kavita_help"), allow_abbrev=False
    )
    mapping.add_argument("username", help=t("cli.profile_username_help"))
    which = mapping.add_mutually_exclusive_group(required=True)
    which.add_argument("--kavita-user", help=t("cli.profile_kavita_user_help"))
    which.add_argument("--clear", action="store_true", help=t("cli.profile_kavita_clear_help"))
    mapping.set_defaults(handler=run_map_kavita)

    remove = actions.add_parser("remove", help=t("cli.profile_remove_help"), allow_abbrev=False)
    remove.add_argument("username", help=t("cli.profile_username_help"))
    remove.set_defaults(handler=run_remove)

    export = actions.add_parser("export", help=t("cli.profile_export_help"), allow_abbrev=False)
    export.add_argument("username", help=t("cli.profile_username_help"))
    export.set_defaults(handler=run_export, configure_logging=False)  # stdout is the document


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


# One literal key per code: the catalogue test reads the keys, and the message never holds more
# than the catalogue's own text (nothing typed, nothing read from the password file).
_REFUSALS: dict[str, Callable[[], str]] = {
    "not_allowed": lambda: t("profiles.not_allowed"),
    "not_found": lambda: t("profiles.not_found"),
    "invalid_role": lambda: t("profiles.invalid_role"),
    "invalid_policy": lambda: t("profiles.invalid_policy"),
    "child_requires_max_age": lambda: t("profiles.child_requires_max_age"),
    "max_age_only_for_child": lambda: t("profiles.max_age_only_for_child"),
    "child_cannot_rate": lambda: t("profiles.child_cannot_rate"),
    "child_source_not_family": lambda: t("profiles.child_source_not_family"),
    "ceiling_above_creator": lambda: t("profiles.ceiling_above_creator"),
    "username_taken": lambda: t("profiles.username_taken"),
    "too_many_admins": lambda: t("profiles.too_many_admins"),
    "last_admin": lambda: t("profiles.last_admin"),
    "cannot_remove_self": lambda: t("profiles.cannot_remove_self"),
    "cannot_change_own_role": lambda: t("profiles.cannot_change_own_role"),
    "changed_meanwhile": lambda: t("profiles.changed_meanwhile"),
    "invalid_visibility": lambda: t("profiles.invalid_visibility"),
    "child_visibility_household": lambda: t("profiles.child_visibility_household"),
    "not_a_child": lambda: t("profiles.not_a_child"),
    "invalid_kavita_username": lambda: t("profiles.invalid_kavita_username"),
    "kavita_username_taken": lambda: t("profiles.kavita_username_taken"),
}


def _refusal(error: ProfileError) -> int:
    return _fail(_REFUSALS.get(error.code, _REFUSALS["not_allowed"])())


def _bad_secret_shape(exc: ValueError) -> int:
    """Only the kind of refusal, chosen from fixed texts: never the value that was refused."""
    if str(exc) == "invalid_username":
        return _fail(t("admin.invalid_username"))
    if str(exc) == "password_too_long":
        return _fail(t("admin.password_too_long"))
    return _fail(t("admin.password_too_short"))


def _profile(auth: IdentityCommands, username: str) -> ProfileRecord | None:
    wanted = username.strip().casefold()
    return next((p for p in auth.profile_list() if p.username.casefold() == wanted), None)


@_command
def run_add(args: argparse.Namespace, auth: IdentityCommands) -> int:
    try:
        password = _read_password(args.password_file)
    except (OSError, UnicodeDecodeError) as exc:
        return _fail(t("admin.password_unreadable", detail=type(exc).__name__))
    try:
        auth.profile_add(
            args.username, password, args.role, max_age_rating=args.max_age,
            can_rate=args.can_rate,
        )  # fmt: skip
    except ProfileError as error:
        return _refusal(error)
    except ValueError as exc:
        return _bad_secret_shape(exc)
    print(t("profiles.created", username=args.username.strip()))
    return 0


@_command
def run_list(args: argparse.Namespace, auth: IdentityCommands) -> int:
    try:
        profiles = auth.profile_list()
    except ProfileError as error:
        return _refusal(error)
    if args.json:
        rows = [
            {"id": p.id, "username": p.username, "role": p.role,
             "max_age_rating": p.max_age_rating, "can_rate": p.can_rate}
            for p in profiles
        ]  # fmt: skip
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not profiles:
        print(t("profiles.none"))
    for p in profiles:
        ceiling = "-" if p.max_age_rating is None else str(p.max_age_rating)
        print(t("profiles.line", username=p.username, role=p.role, ceiling=ceiling,
                rate="oui" if p.can_rate else "non"))  # fmt: skip
    return 0


@_command
def run_set(args: argparse.Namespace, auth: IdentityCommands) -> int:
    try:
        profile = _profile(auth, args.username)
        if profile is None:
            return _fail(t("profiles.unknown", username=args.username))
        if args.role is not None or args.max_age is not None or args.can_rate is not None:
            auth.profile_set(
                profile.id, role=args.role, max_age_rating=args.max_age, can_rate=args.can_rate
            )
        for source, policy in args.source_policy:
            auth.profile_policy_set(profile.id, source, policy)
            print(t("profiles.policy_set", username=profile.username, source=source, policy=policy))
    except ProfileError as error:
        return _refusal(error)
    print(t("profiles.updated", username=profile.username))
    return 0


@_command
def run_map_kavita(args: argparse.Namespace, auth: IdentityCommands) -> int:
    """Name the Kavita account of a child, or clear it (EF-USR-017). A name, never a token."""
    try:
        profile = _profile(auth, args.username)
        if profile is None:
            return _fail(t("profiles.unknown", username=args.username))
        auth.profile_set_kavita_username(profile.id, None if args.clear else args.kavita_user)
    except ProfileError as error:
        return _refusal(error)
    if args.clear:
        print(t("profiles.kavita_cleared", username=profile.username))
    else:
        print(t("profiles.kavita_mapped", username=profile.username))
    return 0


@_command
def run_remove(args: argparse.Namespace, auth: IdentityCommands) -> int:
    try:
        profile = _profile(auth, args.username)
        if profile is None:
            return _fail(t("profiles.unknown", username=args.username))
        auth.profile_remove(profile.id)
    except ProfileError as error:
        return _refusal(error)
    print(t("profiles.removed", username=profile.username))
    return 0


@_command
def run_export(args: argparse.Namespace, auth: IdentityCommands) -> int:
    """The data of one profile as JSON on the standard output (nothing is written to a file: the
    host chooses where it goes)."""
    try:
        profile = _profile(auth, args.username)
        if profile is None:
            return _fail(t("profiles.unknown", username=args.username))
        document = auth.profile_export(profile.id)
    except ProfileError as error:
        return _refusal(error)
    print(json.dumps(document, ensure_ascii=False, indent=2))
    return 0
