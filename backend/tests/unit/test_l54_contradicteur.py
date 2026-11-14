"""M3 L54, contradicteur: red PoCs, see `docs/decisions/M3.md` (« L54 : contradicteur »).

The workflows are read as YAML, never run. Each test states one hole of the PR #253."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.req("ENF-SEC-007")

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
REPO_SCRIPT = re.compile(r"(?:^|[\s;&|(])\.?/?\.github/scripts/[\w.-]+")


def _jobs(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return loaded["jobs"]


@pytest.mark.parametrize("workflow", sorted(p.name for p in WORKFLOWS.glob("*.yml")))
def test_a_job_that_runs_a_script_of_the_repository_checks_it_out_first(workflow: str) -> None:
    """B1: the `merge` job of image-publish.yml runs `.github/scripts/second-registry.sh` with no
    `actions/checkout` before it: the file is absent on the runner, the step exits 127, with or
    without CODEBERG_TOKEN, so every publication on main ends red after the GHCR signature."""
    problems = []
    for name, job in _jobs(workflow).items():
        checked_out = False
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                checked_out = True
            if REPO_SCRIPT.search(str(step.get("run", ""))) and not checked_out:
                problems.append(
                    f"{name}: `{step.get('name')}` runs a repository script, no checkout"
                )
    assert not problems, problems


def _first(steps: list[dict[str, Any]], pattern: str, start: int = 0) -> int:
    for index, step in enumerate(steps[start:], start):
        if re.search(pattern, str(step.get("run", ""))):
            return index
    raise AssertionError(pattern)


def test_ghcr_tags_are_published_only_after_the_signature() -> None:
    """M1 (GHCR): `edge` and `sha-<long>` are created by `imagetools create -t` in the step
    « Create the multi-arch manifest », before the attestations and before `cosign sign`. If
    Fulcio, Rekor or cosign fails, the tags stay on an unsigned digest that users pull."""
    steps = _jobs("image-publish.yml")["merge"]["steps"]
    tagged = _first(steps, r"imagetools create[^\n]*\$\{args\[@\]\}")
    signed = _first(steps, r"cosign sign")
    assert signed < tagged, f"public tags at step {tagged}, signature at step {signed}"


def test_second_registry_tags_are_published_only_after_the_signature() -> None:
    """M1 (second registry): same order, `-t ${SECOND_IMAGE}:edge` is pushed, then signed."""
    steps = _jobs("image-publish.yml")["merge"]["steps"]
    first_second = next(i for i, s in enumerate(steps) if s.get("id") == "second")
    tagged = _first(steps, r"SECOND_IMAGE\}:\$\{tag\}", first_second)
    signed = _first(steps, r"cosign sign[^\n]*SECOND_IMAGE", first_second)
    assert signed < tagged, f"second registry tags at step {tagged}, signature at step {signed}"


def _security_problems() -> Any:
    spec = importlib.util.spec_from_file_location(
        "l54_supply_chain", Path(__file__).with_name("test_supply_chain_m3.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._security_problems


@pytest.mark.parametrize(
    "line",
    [
        "Shelfhold est conforme au Cyber Resilience Act, aucune faille connue.",
        "Le projet est certifié CRA : pas de souci pour les intégrateurs.",
        "Compliant with the CRA (aucune démarche à faire).",
    ],
)
def test_the_compliance_lock_rejects_a_claim_next_to_an_unrelated_negation(line: str) -> None:
    """m1: DENIAL accepts any line holding « aucune » or « pas de », so a plain claim of
    conformity passes as soon as the sentence has an unrelated negation."""
    base = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    problems = _security_problems()(base + "\n" + line + "\n")
    assert any("compliance claim" in p for p in problems), problems


# --- second pass ---

SCRIPT = ROOT / ".github" / "scripts" / "second-registry.sh"
GUARD = 'if [[ "$registry" =~ $re ]] && [[ "$registry" != *..* ]]'


def _hostile_values() -> list[str]:
    """The values of `test_second_registry_hostile_variable_never_reaches_the_outputs`."""
    import ast

    tree = ast.parse(Path(__file__).with_name("test_supply_chain_m3.py").read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and "hostile" in node.name:
            for deco in node.decorator_list:
                if isinstance(deco, ast.Call) and len(deco.args) == 2:
                    return list(ast.literal_eval(deco.args[1]))
    raise AssertionError("hostile list not found")


def _detect(script: Path, tmp_path: Path, registry: str) -> dict[str, str]:
    import os
    import shutil
    import subprocess

    out, summary = tmp_path / "out", tmp_path / "summary"
    out.write_text("")
    summary.write_text("")
    env = {
        "PATH": os.environ["PATH"],
        "GITHUB_OUTPUT": str(out),
        "GITHUB_STEP_SUMMARY": str(summary),
        "HAS_TOKEN": "true",
        "SECOND_REGISTRY": registry,
        "OWNER": "someowner",
        "REPO_NAME": "shelfhold",
    }
    subprocess.run(  # noqa: S603
        [shutil.which("bash") or "/bin/bash", str(script), "detect"],
        env=env,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    pairs = out.read_text("utf-8").splitlines()
    return dict(line.split("=", 1) for line in pairs if "=" in line)


def test_the_hostile_values_still_guard_the_path_validation(tmp_path: Path) -> None:
    """S1: since the host allowlist, every hostile value of the lot's test has a host outside
    the list (evil.example, https:, /…), so it is refused by the allowlist alone. Delete the
    path validation (`$re`, `..`) and that test stays green, while `codeberg.org/a\\nenabled=true`
    or `codeberg.org/../x` would reach GITHUB_OUTPUT next to the token. Mutation: the guard is
    replaced by `if true`; the hostile list must kill the mutant."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert GUARD in text
    mutant = tmp_path / "mutant.sh"
    mutant.write_text(
        re.sub(r'if \[\[ "\$registry" =~ \$re \]\][^\n]*; then', "if true; then", text)
    )
    assert "if true; then" in mutant.read_text(encoding="utf-8")
    killed = [v for v in _hostile_values() if _detect(mutant, tmp_path, v).get("enabled") == "true"]
    assert killed, "no hostile value reaches the path validation: the mutant survives"


@pytest.mark.parametrize(
    "hostile",
    ["codeberg.org/a\nenabled=true", "codeberg.org/../x", "codeberg.org/a b", "quay.io/`id`"],
)
def test_the_real_script_refuses_a_hostile_path_on_an_allowed_host(
    tmp_path: Path, hostile: str
) -> None:
    """S1, other half (green today): what the lot's test should hold."""
    assert _detect(SCRIPT, tmp_path, hostile).get("enabled") != "true"


def test_second_registry_public_tags_are_checked_against_the_signed_digest() -> None:
    """s2: on GHCR the step « Publish the public tags » reads the tag back and compares it with
    INDEX; on the second registry the same step creates `edge`/`sha-<long>` and checks nothing.
    A registry that rewrites the index (format conversion, media types) would carry public tags
    on a digest that nobody signed, in a green job."""
    steps = _jobs("image-publish.yml")["merge"]["steps"]
    last = [
        s
        for s in steps
        if "public tags" in str(s.get("name", "")) and "Second" in str(s.get("name"))
    ]
    assert len(last) == 1
    run = str(last[0]["run"])
    assert "imagetools inspect" in run and '"$INDEX"' in run, run
