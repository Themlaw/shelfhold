"""ENF-SEC-016: the lockfile never drops below a security floor; new M1a dependencies are locked."""

import tomllib
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]

# name -> (inclusive floor, exclusive ceiling or None)
FLOORS: dict[str, tuple[str, str | None]] = {
    "pillow": ("13.0.0", None),
    "starlette": ("1.3.1", None),
    "python-multipart": ("0.0.30", None),
    "apprise": ("2.1", "3"),
    "huey": ("3.5.4", "3.6"),
    "webauthn": ("3.0.1", "4"),  # M1b L00: passkeys (P8), identity.webauthn only
}
M1A_RUNTIME = {
    "huey", "argon2-cffi", "pynacl", "pyrage", "defusedxml", "nh3", "httpx2", "apprise",
    "pillow", "python-multipart", "python-ulid", "langcodes",
}  # fmt: skip
M1B_RUNTIME = {"webauthn"}
M1A_DEV = {"hypothesis", "syrupy", "pytest-xdist", "xmlschema"}


def _version(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in text.split("."):
        digits = "".join(c for c in piece if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts + [0] * (4 - len(parts)))


def _locked() -> dict[str, str]:
    data = tomllib.loads((BACKEND / "uv.lock").read_text(encoding="utf-8"))
    return {p["name"]: p["version"] for p in data["package"]}


def _names(requirements: list[str]) -> set[str]:
    out = set()
    for req in requirements:
        name = req
        for sep in ">=<~![ ;":
            name = name.split(sep)[0]
        out.add(name.lower().replace("_", "-"))
    return out


@pytest.mark.req("ENF-SEC-016")
def test_dependency_floors_enforced_in_lockfile() -> None:
    locked = _locked()
    for name, (floor, ceiling) in FLOORS.items():
        assert name in locked, f"{name} is not in uv.lock"
        assert _version(locked[name]) >= _version(floor), f"{name} {locked[name]} < {floor}"
        if ceiling:
            assert _version(locked[name]) < _version(ceiling), f"{name} {locked[name]} >= {ceiling}"


@pytest.mark.req("ENF-SEC-016")
def test_floors_are_declared_in_pyproject_not_only_in_the_lock() -> None:
    project = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    declared = _names(project["project"]["dependencies"])
    constrained = _names(project["tool"]["uv"]["constraint-dependencies"])
    for name in FLOORS:
        assert name in declared | constrained, f"{name} has no floor in pyproject.toml"
    assert declared >= M1A_RUNTIME | M1B_RUNTIME
    assert _names(project["dependency-groups"]["dev"]) >= M1A_DEV


@pytest.mark.req("ENF-SEC-016")
def test_exclude_newer_supply_chain_window_is_kept() -> None:
    project = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["tool"]["uv"]["exclude-newer"] == "14 days"
