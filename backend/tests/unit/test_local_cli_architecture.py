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


def _offences(source: str) -> list[str]:
    """Every way to produce or name the `local_cli` credential: the member, its value, a call of
    the enum, a lookup by name (`getattr`, subscript)."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr == "LOCAL_CLI":
            found.append("member")
        elif isinstance(node, ast.Constant) and node.value == "local_cli":
            found.append("value")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "Credential":
                found.append("call")
            if (
                isinstance(func, ast.Name)
                and func.id in {"getattr", "vars"}
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "Credential"
            ):
                found.append("getattr")
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "Credential"
        ):
            found.append("subscript")
    return found


def _modules_naming() -> set[str]:
    return {
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*.py")
        if _offences(path.read_text(encoding="utf-8"))
    }


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
@pytest.mark.parametrize(
    "code",
    [
        "Credential.LOCAL_CLI",
        "x = 'local_cli'",
        "if c == 'local_cli': pass",
        "Credential('local_cli')",
        "getattr(Credential, 'LOCAL_' + 'CLI')",
        "Credential['LOCAL_CLI']",
        "vars(Credential)",
    ],
)
def test_the_scan_sees_every_way_to_name_the_credential(code: str) -> None:
    assert _offences(code)


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_local_cli_credential_is_named_only_by_the_socket_path() -> None:
    assert _modules_naming() <= ASSIGNERS


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_only_the_peer_module_builds_a_credential_from_outside() -> None:
    """The enum is called only where a peer becomes a principal (`credentials.py` defines it)."""
    for path in ROOT.rglob("*.py"):
        rel = str(path.relative_to(ROOT))
        if rel in {"interfaces/local_cli/principal.py", "identity/domain/credentials.py"}:
            continue
        assert "call" not in _offences(path.read_text(encoding="utf-8")), rel


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
