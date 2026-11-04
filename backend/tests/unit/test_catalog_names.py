"""EF-BIB-046 (naming rule, I21): person fields never hold commas nor a sort name."""

import pytest
from shelfhold.catalog.domain.names import (
    ContributorName,
    NameKind,
    NoPersonFieldName,
    join_people,
    person_field_name,
)


def _name(text: str, kind: NameKind = NameKind.DISPLAY, **kw: object) -> ContributorName:
    return ContributorName(name=text, lang=str(kw.get("lang", "fr")), script="Latn", kind=kind)


@pytest.mark.req("EF-BIB-046")
def test_authority_form_becomes_natural_order_without_comma() -> None:
    result = person_field_name([_name("Hugo, Victor")])
    assert result.text == "Victor Hugo"
    assert "," not in result.text


@pytest.mark.req("EF-BIB-046")
def test_organisation_comma_is_replaced_and_reported() -> None:
    result = person_field_name([_name("Éditeur, Ltd.")], is_org=True)
    assert result.text == "Éditeur Ltd."
    assert result.commas_replaced is True
    assert person_field_name([_name("Victor Hugo")]).commas_replaced is False


@pytest.mark.req("EF-BIB-046")
def test_sort_name_never_in_person_field() -> None:
    names = [_name("Hugo, Victor", NameKind.SORT), _name("Victor Hugo")]
    assert person_field_name(names).text == "Victor Hugo"
    with pytest.raises(NoPersonFieldName):
        person_field_name([_name("Hugo, Victor", NameKind.SORT)])


@pytest.mark.req("EF-BIB-046")
def test_kind_preference_and_requested_language() -> None:
    names = [
        _name("Haruki Murakami", NameKind.ROMANIZED, lang="ja-Latn"),
        _name("村上春樹", NameKind.NATIVE, lang="ja"),
        _name("Haruki Murakami", NameKind.DISPLAY, lang="fr"),
    ]
    assert person_field_name(names, lang="fr").text == "Haruki Murakami"
    assert person_field_name(names, lang="ja").text == "村上春樹"
    assert person_field_name(names[:2]).text == "村上春樹"


@pytest.mark.req("EF-BIB-046")
def test_people_are_joined_by_comma_space() -> None:
    assert join_people(["Victor Hugo", "Ada Lovelace"]) == "Victor Hugo, Ada Lovelace"
    assert join_people([]) == ""
