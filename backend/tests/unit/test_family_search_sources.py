"""M3 L19 (EF-USR-018): the subjects a native source gives travel to the filter, and the
references of the family search stand for one child's result only."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from shelfhold.acquisition.application.family_search import (
    FamilyRef,
    FamilyRefs,
    InvalidFamilyRefError,
)
from shelfhold.catalog.domain.subject_filter import visible_title
from shelfhold.infrastructure.xml import safe_parse_xml
from shelfhold.plugin_host.adapters.native.gutenberg.catalog import parse_csv
from shelfhold.plugin_host.adapters.native.opds.atom import parse_atom
from shelfhold.plugin_host.adapters.native.opds.json import parse_opds2

ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
<entry><id>urn:1</id><title>Book</title>
<category term="Adventure" label="Adventure stories"/><category term="Fiction"/>
<link rel="http://opds-spec.org/acquisition" type="application/epub+zip" href="/b.epub"/>
</entry></feed>"""


@pytest.mark.req("EF-USR-018")
def test_atom_categories_become_the_subjects_of_an_entry() -> None:
    feed = parse_atom(ATOM, "https://example.test/feed", safe_parse_xml)
    assert feed.entries[0].categories == ("Adventure stories", "Fiction")


@pytest.mark.req("EF-USR-018")
def test_opds2_subjects_become_the_subjects_of_an_entry() -> None:
    body = json.dumps(
        {
            "metadata": {"title": "t"},
            "publications": [
                {
                    "metadata": {
                        "title": "Book",
                        "identifier": "urn:1",
                        "subject": ["Fantasy", {"name": "Dragons"}, 5, ""],
                    },
                    "links": [
                        {
                            "rel": "http://opds-spec.org/acquisition",
                            "href": "/b.epub",
                            "type": "application/epub+zip",
                        }
                    ],
                }
            ],
        }
    ).encode()
    feed = parse_opds2(body, "https://example.test/feed")
    assert feed.entries[0].categories == ("Fantasy", "Dragons")


@pytest.mark.req("EF-USR-018")
def test_the_catalogue_subjects_column_is_read() -> None:
    raw = (
        b"Text#,Type,Issued,Title,Language,Authors,Subjects,LoCC\n"
        b'1,Text,2020-01-01,A Tale,en,"Doe, Jane",Fantasy; Erotica -- History,PR\n'
        b"2,Text,2020-01-01,Other,en,Doe,,PR\n"
    )
    books = parse_csv(raw)
    assert books[0].subjects == ("Fantasy", "Erotica -- History") and books[1].subjects == ()


@pytest.mark.req("EF-USR-014")
def test_a_reference_is_bound_to_its_child_and_expires() -> None:
    clock = [datetime(2026, 11, 12, tzinfo=UTC)]
    refs = FamilyRefs(lambda: clock[0])
    stands_for = FamilyRef("src", "r1", {"entry": "e"})
    token = refs.issue("kid", stands_for)
    assert "src" not in token and "r1" not in token
    assert refs.resolve(token, owner="kid") == stands_for
    for owner, forged in (("sister", token), ("kid", token[:-1]), ("kid", "x" * 500), ("kid", "")):
        with pytest.raises(InvalidFamilyRefError):
            refs.resolve(forged, owner=owner)
    clock[0] += timedelta(hours=2)
    with pytest.raises(InvalidFamilyRefError):
        refs.resolve(token, owner="kid")


@pytest.mark.req("EF-USR-018")
def test_the_title_shown_has_no_invisible_character() -> None:
    assert visible_title("S­ex​ ‮Tale\x00 ") == "Sex Tale"
