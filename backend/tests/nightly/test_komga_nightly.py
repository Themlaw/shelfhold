"""Nightly against the official Komga image (ENF-COMP-001, EF-EXP-020).

Not required to merge a pull request: `.github/workflows/nightly.yml` runs it,
one job per version, with `KOMGA_IMAGE` set. No real content source: every file is
built by the test. Run by hand:
`KOMGA_IMAGE=<image> uv run pytest -m nightly tests/nightly/test_komga_nightly.py`.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.nightly


@pytest.mark.req("ENF-COMP-001")
def test_komga_nightly_series_roundtrip() -> None:
    """A series exported to Komga can be read back with correct metadata."""
