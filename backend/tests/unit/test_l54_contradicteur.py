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
                problems.append(f"{name}: `{step.get('name')}` runs a repository script, no checkout")
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
