"""M4 L47: which exported files get a `document_alias` (EF-LEC-009, ENF-QUAL-005).

A file the export wrote under another hash than the source of its document is found by the hash an
e-reader sends only through an alias; a byte-for-byte copy is the source itself and needs none.
"""

import pytest

from shelfhold.export.application.export_alias import alias_wanted

pytestmark = pytest.mark.req("EF-LEC-009", "ENF-QUAL-005")

SOURCE = "sha256:" + "ab" * 32
RENDERED = "sha256:" + "cd" * 32


def test_a_rendered_file_gets_an_alias() -> None:
    assert alias_wanted(RENDERED, SOURCE) is True


def test_a_byte_for_byte_copy_gets_none() -> None:
    assert alias_wanted(SOURCE, SOURCE) is False


def test_a_document_whose_source_is_unknown_gets_one() -> None:
    """Nothing says the file is the source: the alias costs a row, its absence a lost position."""
    assert alias_wanted(RENDERED, None) is True
