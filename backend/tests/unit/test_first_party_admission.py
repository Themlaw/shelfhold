"""Admission of first-party sources and metadata plugins (ENF-LEG-003)."""

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


@pytest.mark.req("ENF-LEG-003")
def test_terms_url_present_in_manifest() -> None:
    """Every first-party source states the terms it was admitted under (an https URL)."""
    manifests = sorted((REPO / "sources").glob("*/manifest.json"))
    assert manifests
    for path in manifests:
        terms = json.loads(path.read_text(encoding="utf-8")).get("terms_url")
        assert isinstance(terms, str) and terms.startswith("https://"), path.parent.name


@pytest.mark.req("ENF-LEG-003")
def test_metadata_plugin_admission_checklist_in_pr_template() -> None:
    template = (REPO / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
    assert "## Admission d'un plugin de métadonnées" in template
    section = template.split("## Admission d'un plugin de métadonnées", 1)[1]
    for item in (
        "licence libre",
        "`terms_url`",
        "robots",
        "quota",
        "attribution",
        "CODEOWNERS",
        "kit de conformité",
    ):
        assert item in section, item
    doc = (REPO / "docs" / "plugins" / "metadata-provider.md").read_text(encoding="utf-8")
    assert "Liste de contrôle d'admission" in doc
