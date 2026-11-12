"""M3 L20: duplicate suggestions are never grounded on the title alone (EF-BIB-004), a field typed
by hand is locked and manual (EF-BIB-011), the preferred team of an Edition is the one exported
(EF-BIB-005). Every "never" here is an attack that tries to break it."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.team_switch import switch_to_preferred_team
from shelfhold.catalog.application.dedup import REASON, open_suggestions, suggest_duplicates
from shelfhold.catalog.application.external_ids import add_external_id
from shelfhold.catalog.application.fields import AgeFieldError, edit_field, propose_fields
from shelfhold.catalog.domain.external_ids import ExternalId
from shelfhold.catalog.domain.provenance import FieldKey
from shelfhold.catalog.public import Catalog, IncomingUnit
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, storage
from shelfhold.sdk.metadata import FieldProposal, Provenance
from tests.catalog_env import Harness, at, harness

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


class Screen:
    """The Work tombstones: the identifiers it is given as purged."""

    def __init__(self, *purged: tuple[str, str]) -> None:
        self._purged = set(purged)

    def places(self, places: object) -> set[str]:
        return set()

    def work_ids(self, ids: Any) -> set[tuple[str, str]]:
        return {(i.provider, i.value) for i in ids} & self._purged


NO_SCREEN = Screen()


@pytest.fixture
def h() -> Iterator[Harness]:
    with harness() as inside:
        yield inside


def work(h: Harness, title: str, *authors: str, kind: str = "manga") -> str:
    work_id = h.work(kind, title)
    if authors:
        h.catalog.credit_work(work_id, "aut", list(authors))
    return work_id


def works_rows(h: Harness) -> list[dict[str, object]]:
    return h.rows("SELECT id, title FROM work ORDER BY id")


def scan(h: Harness, screen: Screen = NO_SCREEN) -> list[str]:
    return suggest_duplicates(h.connection, screen=screen)


# EF-BIB-004 ----------------------------------------------------------------------------------


@pytest.mark.req("EF-BIB-004")
def test_dedup_suggests_never_merges_on_title_only(h: Harness) -> None:
    first = work(h, "La Voie", "Alice Martin")
    second = work(h, "La Voie", "Alice Martin")
    h.edition(first)
    h.edition(second)
    before = (works_rows(h), h.rows("SELECT id, work_id FROM edition ORDER BY id"))
    created = scan(h)
    assert len(created) == 1  # a suggestion: the same title and a person in common
    (suggestion,) = open_suggestions(h.connection)
    assert {suggestion.a, suggestion.b} == {first, second} and suggestion.state == "open"
    assert suggestion.reason == REASON
    after = (works_rows(h), h.rows("SELECT id, work_id FROM edition ORDER BY id"))
    assert after == before  # never a merge
    assert scan(h) == []  # the same pair is one suggestion, not two


@pytest.mark.req("EF-BIB-004")
def test_same_title_other_authors_is_never_suggested(h: Harness) -> None:
    work(h, "La Voie", "Alice Martin")
    work(h, "La Voie", "Bruno Durand")
    assert scan(h) == []


@pytest.mark.req("EF-BIB-004")
def test_same_title_without_any_author_is_never_suggested(h: Harness) -> None:
    work(h, "La Voie")
    work(h, "La Voie")
    work(h, "La Voie", "Alice Martin")
    assert scan(h) == []  # the title alone is not a ground, even three times


@pytest.mark.req("EF-BIB-004")
def test_homonyms_in_case_or_nfd_with_other_authors_are_never_suggested(h: Harness) -> None:
    nfc = "Éternité"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    work(h, nfc, "Alice Martin")
    work(h, nfd.upper(), "Bruno Durand")
    work(h, nfd.lower(), "Chloe Petit")
    assert scan(h) == []


@pytest.mark.req("EF-BIB-004")
def test_homonyms_in_case_or_nfd_with_one_author_in_common_are_suggested(h: Harness) -> None:
    nfc = "Éternité"
    nfd = unicodedata.normalize("NFD", nfc)
    work(h, nfc, "Alice Martin")
    work(h, nfd.upper(), "Alice Martin")
    assert len(scan(h)) == 1  # the spelling is not a reason to miss a real duplicate


@pytest.mark.req("EF-BIB-004")
def test_a_translation_is_never_suggested_as_a_duplicate(h: Harness) -> None:
    work(h, "Le Petit Prince", "Antoine Saint")
    work(h, "The Little Prince", "Antoine Saint")
    assert scan(h) == []


@pytest.mark.req("EF-BIB-004")
def test_volume_n_and_n_plus_one_are_never_suggested(h: Harness) -> None:
    work(h, "Les Voies, tome 1", "Alice Martin")
    work(h, "Les Voies, tome 2", "Alice Martin")
    series = h.catalog.create_series("Les Voies")
    one = h.catalog.create_work("manga", "Les Voies", series_id=series, position="1")
    two = h.catalog.create_work("manga", "Les Voies", series_id=series, position="2")
    for member in (one, two):
        h.catalog.credit_work(member, "aut", ["Alice Martin"])
    assert scan(h) == []  # same title, same author, two places of one series: two Works


@pytest.mark.req("EF-BIB-004")
def test_works_of_two_kinds_or_two_identifiers_are_never_suggested(h: Harness) -> None:
    novel = work(h, "La Voie", "Alice Martin", kind="novel")
    work(h, "La Voie", "Alice Martin", kind="manga")
    assert scan(h) == []  # a novel and its adaptation are related, not duplicates
    a = work(h, "Le Mur", "Alice Martin")
    b = work(h, "Le Mur", "Alice Martin")
    add_external_id(h.connection, a, ExternalId("work", "anilist", "11"), origin="provider")
    add_external_id(h.connection, b, ExternalId("work", "anilist", "12"), origin="provider")
    assert scan(h) == []  # two identifiers of one provider say two Works
    assert novel  # (kept: the first pair above)


@pytest.mark.req("EF-BIB-004")
def test_a_tombstoned_work_is_never_proposed(h: Harness) -> None:
    purged = work(h, "La Voie", "Alice Martin")
    work(h, "La Voie", "Alice Martin")
    add_external_id(h.connection, purged, ExternalId("work", "anilist", "7"), origin="provider")
    assert scan(h, Screen(("anilist", "7"))) == []
    assert open_suggestions(h.connection) == []


@pytest.mark.req("EF-BIB-004")
def test_a_rejected_suggestion_is_not_reopened(h: Harness) -> None:
    work(h, "La Voie", "Alice Martin")
    work(h, "La Voie", "Alice Martin")
    assert len(scan(h)) == 1
    h.connection.execute(sa.text("UPDATE merge_suggestion SET state = 'rejected'"))
    assert scan(h) == [] and open_suggestions(h.connection) == []


@pytest.mark.req("EF-BIB-004")
def test_dedup_never_touches_a_pair_it_did_not_suggest(h: Harness) -> None:
    work(h, "La Voie", "Alice Martin")
    work(h, "La Voie", "Alice Martin")
    scan(h)
    assert h.scalar("SELECT count(*) FROM work") == 2
    assert h.scalar("SELECT count(*) FROM merge_suggestion WHERE state <> 'open'") == 0


# EF-BIB-011 ----------------------------------------------------------------------------------


def proposal(value: str, provider: str = "openlibrary") -> FieldProposal:
    return FieldProposal(
        entity_type="edition", field="publisher", value=value, confidence=0.9,
        provenance=Provenance(provider=provider, fetched_at=NOW),
    )  # fmt: skip


@pytest.mark.req("EF-BIB-011")
def test_manual_edit_locks_field(h: Harness) -> None:
    edition = h.edition()
    key = FieldKey("edition", edition.id, "publisher")
    propose_fields(h.connection, {"edition": edition.id}, [proposal("Provider House")], now=NOW)
    edit_field(h.connection, key, "Ma maison", now=NOW)
    row = h.rows("SELECT value, provider, provenance, locked FROM metadata_field")[0]
    assert row == {
        "value": '"Ma maison"', "provider": "admin", "provenance": "manual", "locked": 1,
    }  # fmt: skip
    report = propose_fields(
        h.connection, {"edition": edition.id}, [proposal("Another House", "wikidata")], now=NOW
    )
    assert report.kept_locked == (key,) and report.written == ()
    assert h.rows("SELECT value, provenance FROM metadata_field")[0] == {
        "value": '"Ma maison"', "provenance": "manual",
    }  # fmt: skip


@pytest.mark.req("EF-BIB-011")
@pytest.mark.parametrize("name", ["age_rating", "age", "maturity", "content_rating_x", "Age-Min"])
def test_manual_edit_of_an_age_field_is_refused_and_writes_nothing(h: Harness, name: str) -> None:
    edition = h.edition()
    with pytest.raises(AgeFieldError):
        edit_field(h.connection, FieldKey("edition", edition.id, name), "18", now=NOW)
    assert h.scalar("SELECT count(*) FROM metadata_field") == 0


@pytest.mark.req("EF-BIB-011")
@pytest.mark.parametrize("value", ["", "   ", "x" * 5000, "a\x00b"])
def test_manual_edit_refuses_an_absurd_value(h: Harness, value: str) -> None:
    edition = h.edition()
    with pytest.raises(ValueError, match="text of 1 to"):
        edit_field(h.connection, FieldKey("edition", edition.id, "publisher"), value, now=NOW)
    assert h.scalar("SELECT count(*) FROM metadata_field") == 0


# EF-BIB-005 ----------------------------------------------------------------------------------


def two_teams(h: Harness) -> tuple[Any, str, str, str, dict[str, str], dict[str, str]]:
    """One chapter, released by Team A (exported) and Team B, both fetched."""
    edition = h.edition()
    binding_a, binding_b = h.binding(edition), h.binding(edition)
    catalog = Catalog(h.connection)
    unit_id = ""
    variants: dict[str, str] = {}
    contents: dict[str, str] = {}
    for label, binding, minute in (("Team A", binding_a, 1), ("Team B", binding_b, 2)):
        result = catalog.ingest(
            IncomingUnit(
                binding=binding, remote_unit_id=f"r-{label}", raw_number="1",
                seen_at=at(minute), team_label=label, page_count=20, content_hash=digest(label),
            )
        )  # fmt: skip
        unit_id, variants[label] = result.unit_id, result.variant_id
        contents[label] = new_ulid()
        h.connection.execute(
            sa.insert(acquisition.download_request).values(
                id=new_ulid(), target_kind="unit", variant_id=variants[label],
                source_binding_id=binding.id, unit_id=unit_id, state="fetched", updated_at=NOW,
            )
        )  # fmt: skip
        h.connection.execute(
            sa.insert(storage.content).values(
                id=contents[label], unit_id=unit_id, variant_id=variants[label], kind="images",
                media_type="application/vnd.comicbook+zip", format="cbz", retention="passthrough",
                original_sha256=digest(label), store_provenance="received", size=1,
            )
        )  # fmt: skip
    teams = {r["label"]: r["team_id"] for r in h.rows("SELECT team_id, label FROM team_alias")}
    catalog.set_preferred(unit_id, variants["Team A"])
    return edition, unit_id, teams["Team A"], teams["Team B"], variants, contents


def digest(label: str) -> str:
    return "sha256:" + label.encode().hex().ljust(64, "0")


class Upgrader:
    def __init__(self, *outcomes: str) -> None:
        self.outcomes, self.calls = list(outcomes), []  # type: ignore[var-annotated]

    def upgrade_unit(self, unit_id: str, content_id: str) -> list[str]:
        self.calls.append((unit_id, content_id))
        return list(self.outcomes)


def preferred(h: Harness, unit_id: str) -> str:
    return str(h.scalar("SELECT id FROM unit_variant WHERE unit_id = :u AND preferred", u=unit_id))


@pytest.mark.req("EF-BIB-005")
def test_preferred_team_policy(h: Harness) -> None:
    edition, unit_id, team_a, team_b, variants, contents = two_teams(h)
    Catalog(h.connection).prefer_team(edition.id, team_b)
    assert Catalog(h.connection).team_priority(edition.id)[0] == team_b
    upgrader = Upgrader("upgraded")
    report = switch_to_preferred_team(h.connection, edition.id, upgrader=upgrader)
    assert upgrader.calls == [(unit_id, contents["Team B"])]  # the replacement is the upgrade
    assert preferred(h, unit_id) == variants["Team B"]  # and what is exported from now on
    assert report.switched == (unit_id,) and report.blocked == {}
    again = switch_to_preferred_team(h.connection, edition.id, upgrader=upgrader)
    assert again.switched == () and len(upgrader.calls) == 1  # nothing left to switch
    assert team_a != team_b


@pytest.mark.req("EF-BIB-005")
@pytest.mark.parametrize("outcome", ["skipped_read", "needs_confirmation", "root_frozen", "busy"])
def test_a_refused_upgrade_keeps_the_exported_team(h: Harness, outcome: str) -> None:
    edition, unit_id, _a, team_b, variants, _contents = two_teams(h)
    Catalog(h.connection).prefer_team(edition.id, team_b)
    report = switch_to_preferred_team(h.connection, edition.id, upgrader=Upgrader(outcome))
    assert preferred(h, unit_id) == variants["Team A"]  # the served file and the flag agree
    assert report.switched == () and report.blocked == {unit_id: (outcome,)}


@pytest.mark.req("EF-BIB-005")
def test_a_team_of_another_edition_is_never_preferred(h: Harness) -> None:
    edition, _unit, _a, _b, _variants, _contents = two_teams(h)
    other = h.edition()
    foreign = Catalog(h.connection).resolve_team(h.binding(other), "Team Z", at(9)).id
    with pytest.raises(ValueError, match="not a team of this edition"):
        Catalog(h.connection).prefer_team(edition.id, foreign)
