"""Review PoC of #207 (M3 L19, contradicteur): the subject and word filter of the family search.

Each case is red on the branch as it stands: a title or a subject that a child reads as a flagged
word, and that the filter lets through.
"""

from __future__ import annotations

import pkgutil
from pathlib import Path

import pytest

import shelfhold.plugin_host.adapters.native as native
from shelfhold.catalog.domain.subject_filter import clean_terms, is_flagged

# Invisible or look-alike characters: the child reads the word, the filter reads two words or none.
OBFUSCATED = (
    "S\u00adex Tale",  # soft hyphen: invisible
    "Por\u200bn Tale",  # zero width space: invisible
    "He\u200dntai Tale",  # zero width joiner: invisible
    "\u202exes Tale",  # right-to-left override: shown "elaT sex"
    "\u0405\u0435\u0445 Tale",  # Cyrillic letters that look like "Sex"
    "\ua731\u1d07x Tale",  # small capitals
    "S.E.X. Tale",  # one letter a word
    "P0rn Tale",  # a digit for a letter
)


@pytest.mark.req("EF-USR-018")
@pytest.mark.parametrize("title", OBFUSCATED)
def test_review_poc_an_obfuscated_flagged_word_passes_the_filter(title: str) -> None:
    assert is_flagged(title, {}), title


# The usual forms and labels of the default words, as sources write them.
VOCABULARY = (
    ("Sexy Tale", {}),
    ("The Fetishes", {}),
    ("Tale", {"subjects": ["Smut"]}),
    ("Tale", {"subjects": ["Ecchi"]}),
    ("Tale", {"subjects": ["Nudity"]}),
    ("Tale", {"subjects": ["Erotik"]}),  # the same word, another language of the sources
    ("Tale", {"subjects": ["Er\u00f3tico"]}),
)


@pytest.mark.req("EF-USR-018")
@pytest.mark.parametrize(("title", "memo"), VOCABULARY)
def test_review_poc_a_common_form_of_a_flagged_subject_passes_the_filter(
    title: str, memo: dict[str, object]
) -> None:
    assert is_flagged(title, memo), (title, memo)


@pytest.mark.req("EF-USR-018")
def test_review_poc_a_word_of_the_parent_in_another_script_is_dropped_silently() -> None:
    """A parent adds a word in Cyrillic, Greek or Japanese: `clean_terms` folds it to nothing and
    drops it, the PUT answers 200, and a title in that script is never matched (neither by the
    default list, which is Latin only)."""
    for term in ("\u0441\u0435\u043a\u0441", "\u03c3\u03b5\u03be", "\u6210\u4eba\u5411\u3051"):
        assert clean_terms([term]), term
        assert is_flagged(f"{term} 1", {}, [term]), term


@pytest.mark.req("EF-USR-018")
def test_review_poc_no_search_source_shipped_gives_the_subjects_the_filter_reads() -> None:
    """The filter reads `memo["subjects"]` and nothing else; no native source that searches puts
    anything there (the catalogue of one of them has a subjects column it does not read). For every
    source shipped, the subject half of EF-USR-018 is a no-op: only the words of the title count."""
    root = Path(native.__file__).parent
    silent: list[str] = []
    for module in pkgutil.iter_modules([str(root)]):
        folder = root / module.name
        if not module.ispkg:
            continue
        text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.py")))
        if "async def search" in text and "SeriesRef(" in text and '"subjects"' not in text:
            silent.append(f"native source #{len(silent) + 1}")
    assert silent == [], silent
