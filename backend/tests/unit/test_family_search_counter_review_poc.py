"""Counter-review PoC of #207 (M3 L19): forms of a flagged word the folded filter still misses.

Each case is red on the branch as it stands: a title a child reads as a flagged word.
"""

# ruff: noqa: RUF001
from __future__ import annotations

import pytest

from shelfhold.catalog.domain.subject_filter import is_flagged

MISSED = (
    "\U0001f17f\U0001f17e\U0001f181\U0001f17d Tale",  # negative squared letters, NFKC keeps them
    "Pօrn Tale",  # Armenian small oh, looks like "o"
    "ᏚᎬX Tale",  # Cherokee letters that look like "SEX"
    "$ex Tale",  # a sign for a letter
    "P@rn Tale",  # a sign for a letter
)


@pytest.mark.req("EF-USR-018")
@pytest.mark.parametrize("title", MISSED)
def test_counter_review_poc_a_flagged_word_still_passes_the_filter(title: str) -> None:
    assert is_flagged(title, {}), title
