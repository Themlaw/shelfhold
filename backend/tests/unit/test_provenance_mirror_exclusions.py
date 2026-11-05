"""The mirror never reaches Kavita nor a diagnostic bundle (EF-EXP-063, I33, ADR-0046)."""

import pytest

from shelfhold.export.domain.naming import sanitize_name
from shelfhold.export.domain.physical_roots import RESERVED_DIR, is_reserved
from shelfhold.export.domain.provenance_mirror import MIRROR_DIR, mirror_name
from shelfhold.export.targets.kavita.dto import HIDDEN_DIR_PATTERNS
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.logging import redact_source_urls

pytestmark = pytest.mark.req("EF-EXP-063")


def test_mirror_folder_is_covered_by_the_kavita_exclusion_pattern() -> None:
    """L25 adds `**/.shelfhold/**` to the library's ExcludePatterns; the real-Kavita check that
    nothing under it is indexed is the nightly (PLAN section 7)."""
    (pattern,) = HIDDEN_DIR_PATTERNS
    assert pattern == f"**/{RESERVED_DIR}/**"
    assert MIRROR_DIR.startswith(f"{RESERVED_DIR}/")  # under the excluded folder
    assert is_reserved(f"{MIRROR_DIR}/{mirror_name(new_ulid(), new_ulid())}")


@pytest.mark.parametrize("title", [".shelfhold", "  .shelfhold ", ".Shelfhold/manifest", "..", "."])
def test_the_planner_never_names_a_folder_inside_the_reserved_area(title: str) -> None:
    folder = sanitize_name(title)
    assert not folder.startswith(".")
    assert not is_reserved(folder)
    assert not is_reserved(f"{folder}/{folder}.cbz")


def test_diagnostic_bundle_redacts_source_urls() -> None:
    """A bundle holds no mirror (see `is_diagnostic_excluded`) and masks source URLs in what it
    does hold. The bundle itself arrives with the operations lot; it must call these two."""
    text = (
        "fetch https://source.example/series/12?token=abc failed; "
        "memo {'url': 'http://10.0.0.5:8080/api/x'} kept: Dr Stone"
    )
    redacted = redact_source_urls(text)
    assert "source.example" not in redacted and "10.0.0.5" not in redacted
    assert "token=abc" not in redacted
    assert "Dr Stone" in redacted and "failed" in redacted
