"""The SDK never imports the host core (ENF-LEG-010, ENF-QUAL-002).

Moved with the SDK to its own distribution (M6 L02): `shelfhold-sdk` is `sdk/src/shelfhold_sdk`,
and this guard runs with the SDK's own tests (`uv run --project sdk pytest`). Unchanged.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
SDK = SRC / "shelfhold_sdk"

CORE_TOP_LEVEL = {
    "catalog", "acquisition", "library", "export", "identity", "integrations",
    "jobs", "events", "plugin_host", "interfaces", "infrastructure", "config", "i18n", "main",
}  # fmt: skip


def _imported_modules(path: Path, package: list[str] | None = None) -> list[str]:
    """Absolute dotted names imported by an SDK file, relative imports resolved.

    `package` is the dotted package of the file, as a list; by default it is derived from the
    path under src/, which a file elsewhere (a test probe in tmp_path) cannot do.
    """
    if package is None:
        rel = path.relative_to(SRC).with_suffix("")
        package = list(rel.parts) if path.name == "__init__.py" else list(rel.parts[:-1])
        if path.name == "__init__.py":
            package = package[:-1]
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - (node.level - 1)] if node.level else []
            prefix = ".".join(base + ([node.module] if node.module else [])).strip(".")
            found += [f"{prefix}.{alias.name}".strip(".") for alias in node.names]
            found.append(prefix)
    return found


def _is_core(name: str) -> bool:
    parts = name.split(".")
    return parts[0] == "shelfhold" and len(parts) > 1 and parts[1] in CORE_TOP_LEVEL


@pytest.mark.req("ENF-LEG-010", "ENF-QUAL-002")
def test_sdk_never_imports_core() -> None:
    offenders = [
        f"{p.name}: {name}"
        for p in SDK.rglob("*.py")
        for name in _imported_modules(p)
        if _is_core(name)
    ]
    assert not offenders, offenders
