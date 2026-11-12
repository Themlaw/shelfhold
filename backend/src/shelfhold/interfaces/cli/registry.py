"""Registry of CLI command groups.

A group is a module of this package that defines ``register(commands)``: it adds its own
sub-parsers and gives each leaf ``set_defaults(handler=fn)`` where ``fn(args, resolved) -> int``.
Groups are discovered, never listed, so adding one touches no shared file. Set
``configure_logging=False`` in the defaults for a command that must stay quiet (``config show``).
"""

import argparse
import importlib
import os
import pkgutil
import sys
from collections.abc import Callable, Iterable, Sequence
from types import ModuleType

from shelfhold.config import ConfigError, ResolvedSettings, load_settings
from shelfhold.i18n import t
from shelfhold.infrastructure.logging import configure_logging
from shelfhold.infrastructure.settings_store import load_db_settings
from shelfhold.jobs.mode import frozen_on_disk

Handler = Callable[[argparse.Namespace, ResolvedSettings], int]


class DuplicateCommandError(RuntimeError):
    """Two groups registered the same command name."""


def discover_groups() -> list[ModuleType]:
    """Every public module of this package except the registry itself, sorted by name."""
    package = importlib.import_module(__package__ or "shelfhold.interfaces.cli")
    names = sorted(
        info.name
        for info in pkgutil.iter_modules(package.__path__)
        if not info.name.startswith("_") and info.name != "registry"
    )
    return [importlib.import_module(f"{package.__name__}.{name}") for name in names]


def build_parser(groups: Iterable[ModuleType] | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shelfhold", description=t("cli.description"))
    commands = parser.add_subparsers(dest="command", required=True)
    for group in discover_groups() if groups is None else groups:
        try:
            group.register(commands)
        except argparse.ArgumentError as exc:  # argparse refuses a second parser of the same name
            raise DuplicateCommandError(group.__name__, str(exc)) from exc
    label_leaves(parser)
    return parser


# The commands that still run while the maintenance mode is on (EF-INT-018): reading, the exit of
# the mode, what takes an access away (revocations, a password reset by the host), the backup (what
# an operator does in a maintenance), and the two that bring the instance up (`serve` starts frozen,
# `migrate` is an upgrade). Every other leaf of the tree is a write and is refused here, by one
# gate for all of them, whether it goes through the socket, the lock of the instance or neither.
FROZEN_OPEN: frozenset[tuple[str, ...]] = frozenset(
    {
        ("access-table",),
        ("admin", "keys", "list"),
        ("admin", "keys", "revoke"),
        ("admin", "reset-password"),
        ("admin", "sessions", "revoke"),
        ("backup", "run"),
        ("backup", "verify"),
        ("config", "get"),
        ("config", "list"),
        ("config", "show"),
        ("diagnostic", "bundle"),
        ("edition", "attachments"),
        ("edition", "list"),
        ("edition", "merge-proposals"),
        ("export", "item", "show"),
        ("intents", "list"),
        ("jobs", "list"),
        ("maintenance", "off"),
        ("maintenance", "on"),
        ("maintenance", "status"),
        ("migrate",),
        ("missing", "list"),
        ("plugin", "list"),
        ("plugin", "settings"),
        ("profile", "list"),
        ("revisions", "list"),
        ("secrets", "status"),
        ("serve",),
        ("stats",),
        ("status",),
    }
)


def label_leaves(
    parser: argparse.ArgumentParser, path: tuple[str, ...] = ()
) -> list[tuple[str, ...]]:
    """Give every leaf of the tree its words as `command_path` (default of the namespace)."""
    found: list[tuple[str, ...]] = []
    branches = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not branches:
        parser.set_defaults(command_path=path)
        return [path]
    for action in branches:
        for name, sub in action.choices.items():
            found += label_leaves(sub, (*path, name))
    return found


def resolve_settings() -> ResolvedSettings:
    return load_settings(os.environ, db_loader=load_db_settings)


def main(argv: Sequence[str] | None = None) -> int:
    # Before any file is created: database, state and logs are owner-only. The library does not
    # inherit this: its files follow SHELFHOLD_UMASK, applied per file by Fsops (EF-EXP-006).
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    try:
        resolved = resolve_settings()
    except ConfigError as exc:
        print(t("config.invalid", detail=exc), file=sys.stderr)
        return 2
    for path in resolved.world_readable_secret_files:
        print(t("config.secret_file_world_readable", path=path), file=sys.stderr)
    if getattr(args, "configure_logging", True):
        settings = resolved.values
        configure_logging(level=settings.log_level, log_format=settings.log_format)
    if getattr(args, "command_path", ()) not in FROZEN_OPEN and frozen_on_disk(
        resolved.values.database_path
    ):
        print(t("local_cli.maintenance_active"), file=sys.stderr)  # EF-INT-018
        return 1
    handler: Handler = args.handler
    return handler(args, resolved)
