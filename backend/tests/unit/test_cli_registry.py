"""interfaces/cli is a registry of command groups: a new group is a new module."""

from __future__ import annotations

import argparse
import types

import pytest

from shelfhold.interfaces.cli import registry


@pytest.mark.req("ENF-QUAL-002")
def test_groups_are_discovered_from_the_package() -> None:
    names = [m.__name__.rsplit(".", 1)[1] for m in registry.discover_groups()]
    assert {"migrate", "serve", "config"} <= set(names)
    assert names == sorted(names)  # deterministic help output
    assert all(callable(m.register) for m in registry.discover_groups())


@pytest.mark.req("ENF-QUAL-002")
def test_a_new_group_registers_without_editing_the_registry() -> None:
    seen: list[str] = []

    def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        sub = commands.add_parser("probe")
        sub.set_defaults(handler=lambda _args, _resolved: seen.append("ran") or 7)

    group = types.ModuleType("shelfhold.interfaces.cli.probe")
    group.register = register  # type: ignore[attr-defined]
    parser = registry.build_parser([group])
    args = parser.parse_args(["probe"])
    assert args.handler(args, None) == 7
    assert seen == ["ran"]


@pytest.mark.req("ENF-QUAL-002")
def test_duplicate_command_names_are_refused() -> None:
    def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        commands.add_parser("same")

    one = types.ModuleType("shelfhold.interfaces.cli.one")
    two = types.ModuleType("shelfhold.interfaces.cli.two")
    one.register = register  # type: ignore[attr-defined]
    two.register = register  # type: ignore[attr-defined]
    with pytest.raises(registry.DuplicateCommandError):
        registry.build_parser([one, two])


@pytest.mark.req("ENF-QUAL-002")
def test_entry_point_targets_the_registry() -> None:
    import tomllib
    from pathlib import Path

    project = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert project["project"]["scripts"]["shelfhold"] == "shelfhold.interfaces.cli:main"
