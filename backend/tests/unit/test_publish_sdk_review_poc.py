"""Relecture adverse de M6 L03 : défauts de publish-sdk que les tests du lot laissent passer."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_publish_sdk_workflow import _load, _publish_problems, _pypa_step

ROOT = Path(__file__).resolve().parents[3]


def _swap(expression: str, first: str, second: str) -> str:
    return expression.replace(first, "\0").replace(second, first).replace("\0", second)


@pytest.mark.req("ENF-SEC-007")
def test_poc_dispatch_to_real_pypi_is_caught() -> None:
    """Inverser l'aiguillage fait publier un lancement manuel, depuis n'importe quel ref, sur
    PyPI réel : les tests du lot ne vérifient que la présence des chaînes, pas leur sens."""
    workflow = copy.deepcopy(_load("publish-sdk.yml"))
    publish = workflow["jobs"]["publish"]
    publish["environment"]["name"] = _swap(publish["environment"]["name"], "'testpypi'", "'pypi'")
    step = _pypa_step(publish)
    step["with"]["repository-url"] = _swap(
        step["with"]["repository-url"],
        "https://test.pypi.org/legacy/",
        "https://upload.pypi.org/legacy/",
    )
    assert _publish_problems(workflow), "dispatch → pypi/upload.pypi.org not detected"


@pytest.mark.req("ENF-SEC-007")
def test_poc_pypi_environment_bound_to_sdk_tags_documented() -> None:
    """L'environnement `pypi` n'est pas lié aux étiquettes : l'éditeur de confiance PyPI ne
    contrôle que dépôt, fichier de workflow et environnement, pas le ref. Une branche qui
    modifie publish-sdk.yml (déclencheur `push: branches`) obtient un jeton OIDC `pypi` valide,
    seule la relecture manuelle l'arrête. La règle d'étiquettes `release-tags` ne couvre que
    `refs/tags/v*` : `sdk-v*` se crée, se déplace et se supprime librement."""
    section = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8").split(
        "## Publier le SDK", 1
    )[-1]
    # politique de déploiement de l'environnement `pypi` restreinte aux étiquettes sdk-v*
    assert "sdk-v*" in section and "déploiement" in section.lower(), "no deployment tag policy"
    # règle d'étiquettes (ruleset) qui protège sdk-v* comme v*
    assert "refs/tags/sdk-v*" in section, "sdk-v* tags not protected by a ruleset"


@pytest.mark.req("ENF-SEC-007")
def test_poc_published_version_checked_on_built_files() -> None:
    """Le contrôle du tag lit la première ligne `version = "…"` du fichier entier par sed, pas
    `[project].version`, et rien ne compare le tag aux fichiers réellement construits."""
    build = _load("publish-sdk.yml")["jobs"]["build"]
    steps = [str(s.get("name", "")) + str(s.get("run", "")) for s in build["steps"]]
    after_build = steps[next(i for i, s in enumerate(steps) if "uv build" in s) + 1 :]
    assert any("TAG" in s and (".whl" in s or "dist/" in s) for s in after_build), (
        "the tag is never checked against the built distributions"
    )
