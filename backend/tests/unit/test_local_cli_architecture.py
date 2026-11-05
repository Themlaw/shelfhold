"""Only the socket server assigns `local_cli`; nothing in the HTTP stack can (EF-USR-024, I47)."""

import ast
from pathlib import Path

import pytest

import shelfhold

ROOT = Path(shelfhold.__file__).parent
ASSIGNERS = {
    "identity/domain/credentials.py",  # the enum and the axis label
    "identity/domain/reserved.py",  # the verdict for that credential
    "identity/application/service.py",  # the audit actor of that credential
    "interfaces/local_cli/principal.py",  # the only place a peer becomes a principal
}


def _modules_naming(attribute: str) -> set[str]:
    found: set[str] = set()
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == attribute:
                found.add(str(path.relative_to(ROOT)))
    return found


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_local_cli_credential_is_named_only_by_the_socket_path() -> None:
    assert _modules_naming("LOCAL_CLI") <= ASSIGNERS


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_http_interface_never_imports_the_local_cli_or_the_socket() -> None:
    for path in (ROOT / "interfaces" / "http_api").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "local_cli" not in text.replace("`local_cli`", ""), path.name
        assert "local_socket" not in text, path.name


@pytest.mark.req("EF-USR-024")
def test_only_the_socket_package_opens_sockets() -> None:
    for path in ROOT.rglob("*.py"):
        rel = str(path.relative_to(ROOT))
        if rel.startswith(("infrastructure/local_socket/", "infrastructure/fetcher/")):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != "socket" for alias in node.names), rel
            if isinstance(node, ast.ImportFrom):
                assert node.module != "socket", rel
