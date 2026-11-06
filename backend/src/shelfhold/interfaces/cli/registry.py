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
    return parser


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
    handler: Handler = args.handler
    return handler(args, resolved)
