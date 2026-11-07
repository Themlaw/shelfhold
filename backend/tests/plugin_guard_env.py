"""Helpers for the third-party plugin guard tests: synthetic wheels and the real CLI."""

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.sdk.testing.fakes import FakeRangedConnectedService

CODE = "import sys; from shelfhold.interfaces.cli import main; sys.exit(main(sys.argv[1:]))"
PHRASE = "mêmes droits que Shelfhold"
CANARY = "canary-secret-7f3a"


def manifest(key: str) -> dict[str, object]:
    raw = json.loads(FakeRangedConnectedService().manifest.model_dump_json())
    return {**raw, "key": key}


def plugin_source(key: str, probe: Path) -> str:
    return f"""
import importlib.metadata as m
import json
import os
import sys

import httpx2
import shelfhold

snapshot = {{
    "environ": dict(os.environ),
    "argv": list(sys.argv),
    "httpx_file": httpx2.__file__,
    "shelfhold_file": shelfhold.__file__,
    "shelfhold_version": m.version("shelfhold"),
    "group_names": sorted(e.name for e in m.entry_points(group="shelfhold.plugins")),
    "path": list(sys.path),
}}
with open({str(probe)!r}, "a", encoding="utf-8") as out:
    out.write(json.dumps(snapshot) + "\\n")


class Plugin:
    manifest = {manifest(key)!r}


plugin = Plugin()
"""


@dataclass(frozen=True)
class Wheel:
    path: Path
    sha256: str


def build_wheel(
    directory: Path,
    dist: str,
    modules: dict[str, str],
    *,
    entry: tuple[str, str] | None = None,
    extra: dict[str, str] | None = None,
    version: str = "1.0",
) -> Wheel:
    base = dist.replace("-", "_").replace(".", "_")
    info = f"{base}-{version}.dist-info"
    files = {**modules, **(extra or {})}
    files[f"{info}/METADATA"] = f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n"
    files[f"{info}/WHEEL"] = (
        "Wheel-Version: 1.0\nGenerator: t\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    if entry:
        files[f"{info}/entry_points.txt"] = f"[shelfhold.plugins]\n{entry[0]} = {entry[1]}\n"
    files[f"{info}/RECORD"] = "".join(f"{n},,\n" for n in [*files, f"{info}/RECORD"])
    path = directory / f"{base}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return Wheel(path, hashlib.sha256(path.read_bytes()).hexdigest())


def plugin_wheel(directory: Path, dist: str, key: str, probe: Path, module: str) -> Wheel:
    return build_wheel(
        directory,
        dist,
        {f"{module}/__init__.py": plugin_source(key, probe)},
        entry=(key, f"{module}:plugin"),
    )


@dataclass
class Cli:
    env: dict[str, str]
    data: Path

    def run(
        self, *args: str, typed: str = "", **extra_env: str
    ) -> "subprocess.CompletedProcess[str]":
        env = {**self.env, **extra_env}
        return subprocess.run(  # noqa: S603 - the real CLI in a child interpreter
            [sys.executable, "-c", CODE, *args],
            env=env,
            input=typed,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            cwd=self.data.parent,
        )

    def audit_actions(self) -> list[tuple[str, str | None]]:
        engine = create_db_engine(self.data / "shelfhold.db")
        try:
            with engine.connect() as connection:
                rows = connection.execute(
                    sa.text(
                        "SELECT action, target FROM audit WHERE action LIKE 'plugin.%' ORDER BY id"
                    )
                )
                return [(r[0], r[1]) for r in rows]
        finally:
            engine.dispose()


def make_cli(tmp_path: Path) -> Cli:
    data = tmp_path / "data"
    env = {k: v for k, v in os.environ.items() if not k.startswith("SHELFHOLD_")} | {
        "SHELFHOLD_DATA_DIR": str(data),
        "SHELFHOLD_STORE_DIR": str(tmp_path / "store"),
        "SHELFHOLD_API_TOKEN": CANARY,
    }
    cli = Cli(env, data)
    done = cli.run("migrate")
    assert done.returncode == 0, done.stderr
    return cli
