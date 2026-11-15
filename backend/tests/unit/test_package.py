import re

import pytest

import shelfhold
from shelfhold.interfaces.http_api.about import default_source_url, release_tag


@pytest.mark.req("ENF-LEG-012")
def test_package_exposes_a_pep_440_version() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+(rc[1-9]\d*)?", shelfhold.__version__)


@pytest.mark.req("ENF-LEG-012")
@pytest.mark.parametrize(
    ("version", "tag"),
    [
        ("1.0.0rc1", "v1.0.0-rc.1"),
        ("1.0.0rc12", "v1.0.0-rc.12"),
        ("1.0.0", "v1.0.0"),
        ("0.2.0", "v0.2.0"),
    ],
)
def test_release_tag_converts_pep_440_to_the_semver_tag(version: str, tag: str) -> None:
    assert release_tag(version) == tag
    assert default_source_url(version).endswith(f"/tree/{tag}")
