"""M5 L21 (EF-BIB-017): the stored query of a saved selection is a closed structure.

Five criteria (reading state, kind, shelf, language, recent addition), each a short list of
plain values (the last one a number), nothing nested: no SQL, no expression, no operator. A
query is bounded in size and in number of values, and a text that is not that structure is refused
with a reason before anything is stored or evaluated.
"""

import json

import pytest

from shelfhold.catalog.domain.selection_query import (
    MAX_QUERY_BYTES,
    MAX_VALUES,
    QueryRefusedError,
    SelectionQuery,
    dump_query,
    parse_query,
)

pytestmark = pytest.mark.req("EF-BIB-017")

SHELF = "01HZZZZZZZZZZZZZZZZZZZZZZA"
OTHER = "01HZZZZZZZZZZZZZZZZZZZZZZB"


def _refused(raw: object) -> str:
    with pytest.raises(QueryRefusedError) as caught:
        parse_query(raw)  # type: ignore[arg-type]
    return caught.value.reason


def test_selection_query_grammar_closed() -> None:
    """Only the five criteria exist; anything else, whatever it looks like, is refused."""
    for unknown in ("sql", "where", "$or", "__proto__", "title", "work_id", "limit", "order"):
        assert _refused({"kind": ["manga"], unknown: ["x"]}) == "unknown_field", unknown
    # no expression, no operator, no nesting: a value is plain text, a number or nothing
    nested = (
        {"kind": [["manga"]]},
        {"kind": {"in": ["manga"]}},
        {"kind": [{"$ne": "manga"}]},
        {"reading_state": "unread"},  # a list, always
        {"added_within_days": [30]},
        {"added_within_days": {"gt": 3}},
        {"lang": None},
    )
    for query in nested:
        assert _refused(query) == "bad_value", query
    # text that is code, not a value
    for sql in ("manga' OR '1'='1", "x; DROP TABLE work;--", "1=1", "manga OR 1", "a b", "é", ""):
        assert _refused({"kind": [sql]}) == "bad_value", sql
    assert _refused({"shelf": ["not-an-id"]}) == "bad_value"
    assert _refused({"shelf": [SHELF.lower()]}) == "bad_value"
    assert _refused({"lang": ["fr; select 1"]}) == "bad_value"
    assert _refused({"reading_state": ["archived"]}) == "bad_value"
    for days in (0, -1, 3651, 1.5, "30", True, None):
        assert _refused({"added_within_days": days}) == "bad_value", days


def test_selection_query_accepts_the_five_criteria_and_dumps_them_canonically() -> None:
    parsed = parse_query(
        {
            "reading_state": ["read", "unread", "read"],
            "kind": ["manga", "comic"],
            "shelf": [OTHER, SHELF],
            "lang": ["fr", "EN", "pt-BR"],
            "added_within_days": 30,
        }
    )
    assert isinstance(parsed, SelectionQuery)
    assert parsed.reading_state == ("read", "unread")  # the two states, ordered, no repeat
    assert parsed.kind == ("comic", "manga")
    assert parsed.shelf == (SHELF, OTHER)
    assert parsed.lang == ("en", "fr", "pt-br")  # a language is written the way the catalogue does
    assert parsed.added_within_days == 30
    text = dump_query(parsed)
    assert json.loads(text) == {
        "added_within_days": 30, "kind": ["comic", "manga"], "lang": ["en", "fr", "pt-br"],
        "reading_state": ["read", "unread"], "shelf": [SHELF, OTHER],
    }  # fmt: skip
    assert parse_query(text) == parsed  # what is stored reads back the same
    assert dump_query(parse_query(text)) == text
    assert parse_query({"kind": ["manga"]}).shelf == ()  # a criterion left out is no criterion


def test_selection_query_is_bounded_in_size_values_and_depth() -> None:
    assert _refused({}) == "empty"
    assert _refused({"kind": []}) == "empty"
    many = [f"kind{i}" for i in range(MAX_VALUES + 1)]
    assert _refused({"kind": many}) == "too_many_values"
    assert len(parse_query({"kind": many[:MAX_VALUES]}).kind) == MAX_VALUES
    assert _refused("x" * (MAX_QUERY_BYTES + 1)) == "too_big"
    assert _refused(json.dumps({"kind": ["a" * 64] * 40, "lang": ["fr"]})) == "too_big"
    assert _refused("[" * 100_000) == "too_big"  # refused on its length, never parsed
    assert _refused("[" * 1500) == "too_deep"  # short enough to be read, too deep to be a query
    assert _refused('{"kind": [' * 200) == "too_deep"  # one object and four lists are all there is


def test_selection_query_text_must_be_one_json_object() -> None:
    assert _refused("not json") == "not_json"
    assert _refused('{"kind": ["manga"]} trailing') == "not_json"
    assert _refused('{"kind": ["manga"], "kind": ["comic"]}') == "bad_value"  # a repeated key
    for not_an_object in ("[]", "null", "3", '"kind"', "true"):
        assert _refused(not_an_object) == "not_an_object", not_an_object
    assert _refused(b"\xff\xfe") == "not_json"
    assert parse_query(b'{"kind": ["manga"]}').kind == ("manga",)
