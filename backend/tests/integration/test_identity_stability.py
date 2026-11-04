"""Teams belong to the Edition and survive a relink (EF-BIB-035); keys are never recomputed."""

import pytest

from shelfhold.catalog.public import IncomingUnit
from tests.catalog_env import at, harness


def seen(binding, remote: str, raw: str, minute: int, team: str) -> IncomingUnit:  # type: ignore[no-untyped-def]
    return IncomingUnit(
        binding=binding, remote_unit_id=remote, raw_number=raw, seen_at=at(minute), team_label=team
    )


@pytest.mark.req("EF-BIB-035")
def test_team_labels_are_scoped_per_series() -> None:
    with harness() as h:
        one, two = h.edition(), h.edition()
        a, b = h.binding(one), h.binding(two)
        first = h.catalog.resolve_team(a, "Alpha", at(0))
        second = h.catalog.resolve_team(b, "Alpha", at(0))
        assert first.id != second.id  # no merge across series
        assert first.edition_id == one.id and second.edition_id == two.id
        h.catalog.set_team_priority(one.id, [first.id])
        assert (
            h.catalog.team_priority(two.id) == []
        )  # one series' preference leaves the other alone


@pytest.mark.req("EF-BIB-035")
def test_same_label_two_series_two_teams() -> None:
    with harness() as h:
        one, two = h.edition(), h.edition()
        a, b = h.binding(one), h.binding(two)
        h.catalog.ingest(seen(a, "1", "1", 0, "Alpha"))
        h.catalog.ingest(seen(b, "1", "1", 0, "  ALPHA "))
        assert h.scalar("SELECT count(DISTINCT id) FROM team WHERE label = 'alpha'") == 2


@pytest.mark.req("EF-BIB-035")
def test_relink_keeps_team_identity() -> None:
    with harness() as h:
        edition = h.edition()
        old = h.binding(edition)
        alpha = h.catalog.resolve_team(old, "Alpha", at(0))
        beta = h.catalog.resolve_team(old, "Beta", at(0))
        h.catalog.set_team_priority(edition.id, [alpha.id, beta.id])
        h.deactivate(old)
        new = h.binding(edition)  # the relink: a new binding on the same Edition
        again = h.catalog.resolve_team(new, "alpha", at(10))
        assert again.id == alpha.id and not again.created
        assert h.catalog.team_priority(edition.id) == [alpha.id, beta.id]
        aliases = h.rows("SELECT source_binding_id FROM team_alias WHERE team_id = :t", t=alpha.id)
        assert {r["source_binding_id"] for r in aliases} == {old.id, new.id}
        assert h.scalar("SELECT count(*) FROM team") == 2


@pytest.mark.req("EF-BIB-035")
def test_relink_then_next_chapter_same_team() -> None:
    with harness() as h:
        edition = h.edition()
        old = h.binding(edition)
        for n in range(1, 4):
            h.catalog.ingest(seen(old, f"o{n}", str(n), n, "Alpha"))
        h.deactivate(old)
        new = h.binding(edition)
        got = h.catalog.ingest(seen(new, "n4", "4", 20, "Alpha"))
        teams = h.rows("SELECT DISTINCT team_id FROM unit_variant")
        assert len(teams) == 1 and teams[0]["team_id"] == got.team_id
        assert h.catalog.elect_preferred(got.unit_id) == got.variant_id


@pytest.mark.req("EF-BIB-035")
def test_team_of_another_edition_is_refused() -> None:
    with harness() as h:
        one, two = h.edition(), h.edition()
        stranger = h.binding(two)
        with pytest.raises(ValueError, match="edition"):
            h.catalog.ingest(
                IncomingUnit(
                    binding=type(stranger)(id=stranger.id, edition_id=one.id),
                    remote_unit_id="x",
                    raw_number="1",
                    seen_at=at(0),
                )
            )


@pytest.mark.req("EF-BIB-001", "ENF-I18N-005")
def test_equivalent_language_tags_share_one_edition() -> None:
    with harness() as h:
        work = h.work()
        a = h.edition(work, lang="en_us")
        b = h.edition(work, lang="EN-us")
        assert a.id == b.id
        assert h.rows("SELECT lang, lang_raw FROM edition") == [
            {"lang": "en-US", "lang_raw": "en_us"}
        ]
        with pytest.raises(ValueError):
            h.edition(work, lang="multi")


@pytest.mark.req("ENF-OPS-021")
def test_identity_keys_keep_normalizer_version_and_raw() -> None:
    with harness() as h:
        edition = h.edition()
        binding = h.binding(edition)
        h.catalog.ingest(seen(binding, "1", "Ch. 010,50", 0, "Éa  Team"))
        unit = h.rows("SELECT number_raw, number_norm, number_norm_by FROM unit")[0]
        assert (unit["number_raw"], unit["number_norm"]) == ("Ch. 010,50", "10.5")
        assert unit["number_norm_by"] == "number@1"
        team = h.rows("SELECT label, label_raw, label_norm_by FROM team")[0]
        assert team["label_raw"] == "Éa  Team" and team["label"] == "éa team"
        assert team["label_norm_by"] == "team_label@1"
