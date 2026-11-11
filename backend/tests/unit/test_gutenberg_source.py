"""M2 L23 (EF-SRC-007, ENF-LEG-003): the manifest of the Gutenberg source, its canary
configuration and the conformance kit. No test here, and no canary, calls the website."""

from __future__ import annotations

import asyncio
import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from shelfhold.plugin_host.adapters.native.gutenberg import GutenbergSource, build_manifest
from shelfhold.sdk.testing.fakes import FakeFetcher
from shelfhold.sdk.testing.kit import run_source_conformance
from shelfhold.sdk.fetcher import Response
from tests.gutenberg_env import CATALOG_URL, CSV, MIRROR_URL

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "backend/src/shelfhold/plugin_host/adapters/native/gutenberg"
WEBSITE = re.compile(r"gutenberg\.org", re.IGNORECASE)


@pytest.mark.req("EF-SRC-007", "ENF-LEG-003")
def test_the_manifest_file_is_the_one_the_source_declares() -> None:
    on_disk = json.loads((ROOT / "sources/gutenberg/manifest.json").read_text(encoding="utf-8"))
    assert on_disk == json.loads(build_manifest().model_dump_json())


@pytest.mark.req("ENF-LEG-003")
def test_the_manifest_declares_terms_robots_attribution_and_a_slow_rate() -> None:
    m = build_manifest()
    assert m.terms_url and m.respects_robots and m.attribution and m.rate_limit.concurrency == 1
    assert m.rate_limit.requests_per_second <= 0.5
    assert m.url_patterns == [] and m.content_kinds == {"file"}


@pytest.mark.req("EF-SRC-007", "EF-BIB-051")
def test_the_manifest_never_vouches_for_the_files_of_a_mirror() -> None:
    """The files come from a mirror the admin chose: always the untrusted chain, whoever signs."""
    m = build_manifest()
    assert m.content_trust == "untrusted"
    assert m.rights_basis == "us_public_domain" and m.rights_jurisdiction == "US"
    assert m.translation_nature == "published"


@pytest.mark.req("EF-SRC-007", "ENF-LEG-003")
def test_no_host_is_a_default_in_the_manifest_or_the_code() -> None:
    """Every declared host is a placeholder that resolves nowhere until the admin sets the
    address; the website is not written as a URL anywhere in the package."""
    m = build_manifest()
    assert m.permissions.domains and all(d.pattern.endswith(".invalid") for d in m.permissions.domains)
    assert {d.base_url_setting for d in m.permissions.domains} == {"catalog_url", "mirror_url"}
    assert set(m.settings_schema["required"]) == {"catalog_url", "mirror_url"}
    for path in PACKAGE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"https?://[^\s'\"]*gutenberg\.org", text, re.IGNORECASE), path


@pytest.mark.req("ENF-LEG-003")
def test_canary_never_targets_gutenberg_org() -> None:
    """Configuration check (D12): the list of canary targets exists, is empty or free of the
    website, and no source case of the manifest is a canary."""
    config = tomllib.loads((ROOT / "tools/canary_targets.toml").read_text(encoding="utf-8"))
    targets = config.get("targets", [])
    assert isinstance(targets, list)
    for target in targets:
        assert not WEBSITE.search(json.dumps(target)), target
    assert not any(case.canary for case in build_manifest().tests)
    workflow = ROOT / ".github/workflows/canary.yml"
    if workflow.exists():
        assert not WEBSITE.search(workflow.read_text(encoding="utf-8"))


@pytest.mark.req("EF-SRC-007", "EF-SRC-001")
def test_the_source_passes_the_conformance_kit_with_a_scripted_fetcher() -> None:
    """The kit runs every call under the direct-I/O detector (L05) with no network at all."""
    settings = {"catalog_url": CATALOG_URL, "mirror_url": MIRROR_URL}
    responses = {("GET", CATALOG_URL): Response(status=200, body=CSV.encode())}

    def scripted() -> Any:
        fetcher = FakeFetcher(responses, settings=settings)
        return fetcher

    report = asyncio.run(
        run_source_conformance(
            GutenbergSource(), scripted(), reinstall=GutenbergSource
        )
    )
    assert report.ok, report.failures
