"""ENF-QUAL-006: the identity invariants of 02 section 3.1 bis, on a real migrated SQLite database.

I1 (places and variants), I2 (N teams x M chapters), I3 (one exported Variant per Unit), I4 (the
oldest Variant keeps the place), I5 and I27 (a Team belongs to one Edition), I15 (no nullable key
column), I23 (canonical languages), I29 (normalizer version and raw value kept), I32 (a
`remote_unit_id` is never rewritten, revisions are append-only), I34 (watermark and place share
the segment).
"""

import random
from contextlib import ExitStack
from dataclasses import dataclass, replace
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
    run_state_machine_as_test,
)

from shelfhold.catalog.domain.numbering import (
    NUMBER_NORMALIZER_ID,
    NumberError,
    UnitNumber,
    follow_from,
    is_beyond,
    parse_number,
)
from shelfhold.catalog.domain.placement import place_of
from shelfhold.catalog.lang import LanguageError, normalize_lang
from shelfhold.catalog.public import BindingRef, EditionRef, IncomingUnit, IngestOutcome
from tests.catalog_env import Harness, at, harness

EDITION_KEY = (
    "work_id",
    "lang",
    "translation_type",
    "media",
    "publisher",
    "text_version",
    "performance",
    "packaging",
)


def check_i1(h: Harness) -> None:
    assert (
        h.scalar(
            "SELECT count(*) FROM (SELECT 1 FROM unit WHERE track <> 'unnumbered' "
            "GROUP BY edition_id, track, number_segment, number_norm, part HAVING count(*) > 1)"
        )
        == 0
    )
    assert (
        h.scalar(
            "SELECT count(*) FROM (SELECT 1 FROM unit_variant GROUP BY source_binding_id, "
            "remote_unit_id HAVING count(*) > 1)"
        )
        == 0
    )
    assert h.scalar("SELECT count(*) FROM unit_variant WHERE unit_id IS NULL") == 0
    assert (
        h.scalar(
            "SELECT count(*) FROM unit u WHERE NOT EXISTS "
            "(SELECT 1 FROM unit_variant v WHERE v.unit_id = u.id)"
        )
        == 0
    )  # a Unit exists only because a Variant fills it


def check_i3(h: Harness) -> None:
    assert (
        h.scalar(
            "SELECT count(*) FROM (SELECT 1 FROM unit_variant WHERE preferred "
            "GROUP BY unit_id HAVING count(*) > 1)"
        )
        == 0
    )
    assert (
        h.scalar("SELECT count(*) FROM unit_variant WHERE preferred AND removed_at IS NOT NULL")
        == 0
    )


def check_i5_i27(h: Harness) -> None:
    assert (
        h.scalar(
            "SELECT count(*) FROM unit_variant v JOIN unit u ON u.id = v.unit_id "
            "JOIN source_binding b ON b.id = v.source_binding_id WHERE b.edition_id <> u.edition_id"
        )
        == 0
    )
    assert (
        h.scalar(
            "SELECT count(*) FROM unit_variant v JOIN unit u ON u.id = v.unit_id "
            "JOIN team t ON t.id = v.team_id WHERE t.edition_id <> u.edition_id"
        )
        == 0
    )
    assert (
        h.scalar(
            "SELECT count(*) FROM team_alias a JOIN team t ON t.id = a.team_id "
            "JOIN source_binding b ON b.id = a.source_binding_id WHERE b.edition_id <> t.edition_id"
        )
        == 0
    )
    assert (
        h.scalar(
            "SELECT count(*) FROM (SELECT 1 FROM team GROUP BY edition_id, label "
            "HAVING count(*) > 1)"
        )
        == 0
    )


def check_i15(h: Harness) -> None:
    info = {r["name"]: r for r in h.rows("PRAGMA table_info(edition)")}
    assert all(info[c]["notnull"] == 1 for c in EDITION_KEY)
    for column in EDITION_KEY:
        assert h.scalar(f"SELECT count(*) FROM edition WHERE {column} IS NULL") == 0  # noqa: S608


def check_i23(h: Harness) -> None:
    for row in h.rows("SELECT lang, lang_raw FROM edition"):
        assert row["lang"] == normalize_lang(str(row["lang_raw"])).tag
        assert str(row["lang"]).lower() not in {"all", "multi", "und", "mul"}
    for row in h.rows("SELECT lang, lang_raw FROM unit_variant"):
        assert row["lang"] == expected_variant_lang(row["lang_raw"])  # type: ignore[arg-type]


def expected_variant_lang(raw: str | None) -> str:
    """`und` unless the source gave a tag `normalize_lang()` accepts (a placeholder is not one)."""
    if raw is None:
        return "und"
    try:
        return str(normalize_lang(raw).tag)
    except LanguageError:
        return "und"


def check_i29(h: Harness) -> None:
    for row in h.rows("SELECT number_raw, number_norm, number_norm_by FROM unit"):
        assert row["number_norm_by"] == NUMBER_NORMALIZER_ID
        try:
            expected = parse_number(str(row["number_raw"])).norm
        except NumberError:
            expected = ""
        assert row["number_norm"] == expected  # the stored key is the version's own output
    for row in h.rows("SELECT label, label_raw, label_norm_by FROM team"):
        assert row["label_raw"] is not None and row["label_norm_by"] == "team_label@1"
        assert row["label"] == " ".join(str(row["label_raw"]).casefold().split())


def parsed(raw: object) -> UnitNumber | None:
    """What the normalizer makes of a stored raw number (`None`: unreadable, so unnumbered)."""
    try:
        return parse_number(str(raw))
    except NumberError:
        return None


def check_i34(h: Harness) -> None:
    """The place carries the segment the normalizer finds in the raw number, and the watermark
    comparison (segment ordinal first) agrees with the pair computed from that raw number."""
    units = h.rows(
        "SELECT id, edition_id, number_raw, number_segment, number_segment_ordinal FROM unit"
    )
    for row in units:
        p = parsed(row["number_raw"])
        assert row["number_segment"] == (p.segment if p else "")
        assert row["number_segment_ordinal"] == (p.segment_ordinal if p else 0)
    for edition in h.rows("SELECT id FROM edition"):
        for mark in ((0, Decimal(0)), (1, Decimal(2)), (2, Decimal(1))):
            expected = set()
            for row in units:
                p = parsed(row["number_raw"])
                if row["edition_id"] == edition["id"] and p and p.norm and is_beyond(p, mark):
                    expected.add(str(row["id"]))
            assert set(h.catalog.units_beyond(str(edition["id"]), mark)) == expected


def check_i32(h: Harness) -> None:
    for v in h.rows(
        "SELECT id, revision, supersedes_variant_id, source_binding_id FROM unit_variant"
    ):
        revisions = [
            r["revision"]
            for r in h.rows(
                "SELECT revision FROM variant_revision WHERE variant_id = :i ORDER BY revision",
                i=v["id"],
            )
        ]
        assert revisions == list(range(1, int(v["revision"]) + 1))  # type: ignore[call-overload]
        if v["supersedes_variant_id"] is not None:
            old = h.rows(
                "SELECT source_binding_id FROM unit_variant WHERE id = :i",
                i=v["supersedes_variant_id"],
            )
            assert old and old[0]["source_binding_id"] == v["source_binding_id"]


@dataclass
class Shadow:
    """What the test believes about a Variant, computed apart from the repositories."""

    id: str
    binding: str
    remote: str
    unit_id: str
    place: tuple[object, ...]
    team: str | None
    hint: str | None
    arrival: int
    removed: bool = False


def shadow_place(edition_id: str, raw: str, order: int) -> tuple[object, ...]:
    try:
        number = parse_number(raw, source_order=order)
    except NumberError:
        number = replace(parse_number(None, source_order=order), raw=raw)
    p = place_of(number)
    if p.unnumbered:
        return (edition_id, "unnumbered", p.source_order)
    return (edition_id, p.track, p.segment, p.number_norm, p.part)


def shadow_team(label: str | None) -> str | None:
    return " ".join(label.casefold().split()) if label and label.strip() else None


class ModelInvariants(RuleBasedStateMachine):
    """Random histories: a shadow model predicts each outcome, invariants checked every step."""

    def __init__(self) -> None:
        super().__init__()
        self.stack = ExitStack()
        self.h = self.stack.enter_context(harness())
        self.editions: list[EditionRef] = []
        self.bindings: dict[str, list[BindingRef]] = {}
        self.clock = 0
        self.shadow: dict[str, Shadow] = {}
        self.by_key: dict[tuple[str, str], str] = {}
        self.in_conflict: set[str] = set()  # every Variant named by an open conflict
        self.newcomers: set[str] = set()  # the one of each pair that arrived in the conflict

    def teardown(self) -> None:
        self.stack.close()

    @initialize(lang=st.sampled_from(["en", "fr", "ja", "en_us", "zh-TW"]))
    def first_edition(self, lang: str) -> None:
        self.add_edition(lang=lang)

    @rule(
        lang=st.sampled_from(["en", "EN", "fr", "pt_BR", "zh-CN"]),
        kind=st.sampled_from(["manga", "novel"]),
    )
    def add_edition(self, lang: str, kind: str = "manga") -> None:
        edition = self.h.edition(
            kind=kind, lang=lang, media="text" if kind == "novel" else "images"
        )
        if all(e.id != edition.id for e in self.editions):
            self.editions.append(edition)
            self.bindings[edition.id] = [self.h.binding(edition)]

    @rule(i=st.integers(0, 5))
    def relink(self, i: int) -> None:
        edition = self.editions[i % len(self.editions)]
        current = self.bindings[edition.id]
        self.h.deactivate(current[-1])
        current.append(self.h.binding(edition))

    def peers(
        self, s_binding: str, team: str | None, place: tuple[object, ...], skip: str = ""
    ) -> list[Shadow]:
        return [
            o
            for o in self.shadow.values()
            if (o.binding, o.team, o.place) == (s_binding, team, place) and o.id != skip
        ]

    @rule(
        i=st.integers(0, 5),
        b=st.integers(0, 3),
        remote=st.sampled_from(["a", "b", "c", "d"]),
        raw=st.sampled_from(
            [
                "1",
                "2",
                "2",
                "S1 Ch.1",
                "S2 Ch.1",
                "S10 Ch.3",
                "Extra",
                "Ch. 010,50",
                "-4",
                "",
                "3 part 2",
            ]
        ),
        team=st.sampled_from([None, "A", "B", " a "]),
        order=st.integers(0, 3),
        hint=st.sampled_from([None, "v1", "v2"]),
        lang=st.sampled_from([None, "en_us", "multi", "ja"]),
    )
    def ingest(
        self,
        i: int,
        b: int,
        remote: str,
        raw: str,
        team: str | None,
        order: int,
        hint: str | None,
        lang: str | None,
    ) -> None:
        edition = self.editions[i % len(self.editions)]
        binding = self.bindings[edition.id][b % len(self.bindings[edition.id])]
        self.clock += 1
        known = self.by_key.get((binding.id, remote))
        place = shadow_place(edition.id, raw, order)
        key = shadow_team(team)
        # prediction, before the call
        rival: list[Shadow] = []
        if known:
            me = self.shadow[known]
            rival = [o for o in self.peers(me.binding, me.team, me.place, known) if not o.removed]
            changed = hint is not None and hint != me.hint
            expected = IngestOutcome.REVISION if changed else IngestOutcome.UNCHANGED
            opens_conflict = me.removed and bool(rival)
        else:
            same = self.peers(binding.id, key, place)
            live = [o for o in same if not o.removed]
            gone = [o for o in same if o.removed]
            expected = (
                IngestOutcome.NUMBERING_CONFLICT
                if live
                else IngestOutcome.SUPERSEDING
                if gone
                else IngestOutcome.NEW_VARIANT
            )
            opens_conflict = bool(live)
        result = self.h.catalog.ingest(
            IncomingUnit(
                binding=binding,
                remote_unit_id=remote,
                raw_number=raw,
                seen_at=at(self.clock),
                source_order=order,
                team_label=team,
                revision_hint=hint,
                lang=lang,
            )
        )
        assert result.outcome == expected
        assert (result.suggestion_id is not None) == opens_conflict
        if known:
            assert result.variant_id == known  # a known pair is the same Variant
            me.removed = False
            if expected is IngestOutcome.REVISION:
                me.hint = hint
            if opens_conflict:
                older = min(rival, key=lambda o: o.arrival)
                self.in_conflict |= {older.id, known}
                self.newcomers.add(max((older, me), key=lambda o: o.arrival).id)
            return
        if expected is IngestOutcome.NUMBERING_CONFLICT:
            older = min(live, key=lambda o: o.arrival)
            self.in_conflict |= {older.id, result.variant_id}
            self.newcomers.add(result.variant_id)
        if expected is IngestOutcome.SUPERSEDING:
            assert result.supersedes_variant_id == max(gone, key=lambda o: o.arrival).id
        else:
            assert result.supersedes_variant_id is None
        self.shadow[result.variant_id] = Shadow(
            result.variant_id, binding.id, remote, result.unit_id, place, key, hint, self.clock
        )
        self.by_key[(binding.id, remote)] = result.variant_id

    @rule(n=st.integers(0, 40))
    def remove(self, n: int) -> None:
        if self.shadow:
            variant = sorted(self.shadow)[n % len(self.shadow)]
            self.clock += 1
            self.h.catalog.mark_removed(variant, at(self.clock))
            self.shadow[variant].removed = True

    @rule(n=st.integers(0, 40))
    def elect(self, n: int) -> None:
        units = sorted({s.unit_id for s in self.shadow.values()})
        if not units:
            return
        unit_id = units[n % len(units)]
        ranks = {
            str(r["team_id"]): int(r["rank"])  # type: ignore[call-overload]
            for r in self.h.rows(
                "SELECT p.team_id, p.rank FROM team_priority p "
                "JOIN unit u ON u.edition_id = p.edition_id WHERE u.id = :u",
                u=unit_id,
            )
        }
        team_of = {
            str(r["id"]): r["team_id"]
            for r in self.h.rows(
                "SELECT id, team_id FROM unit_variant WHERE unit_id = :u", u=unit_id
            )
        }
        candidates = [
            s
            for s in self.shadow.values()
            if s.unit_id == unit_id and not s.removed and s.id not in self.newcomers
        ]
        oracle = min(
            candidates,
            key=lambda s: (
                ranks.get(str(team_of[s.id])) is None,
                ranks.get(str(team_of[s.id]), 0),
                s.arrival,
            ),
            default=None,
        )
        chosen = self.h.catalog.elect_preferred(unit_id)
        assert chosen == (oracle.id if oracle else None)
        flagged = [
            str(r["id"])
            for r in self.h.rows(
                "SELECT id FROM unit_variant WHERE unit_id = :u AND preferred",
                u=unit_id,
            )
        ]
        assert flagged == ([oracle.id] if oracle else [])

    @rule(i=st.integers(0, 5), swap=st.booleans())
    def prioritise(self, i: int, swap: bool) -> None:
        edition = self.editions[i % len(self.editions)]
        teams = [
            str(r["id"])
            for r in self.h.rows(
                "SELECT id FROM team WHERE edition_id = :e ORDER BY id", e=edition.id
            )
        ]
        if teams:
            self.h.catalog.set_team_priority(edition.id, teams[::-1] if swap else teams)

    @invariant()
    def identity_invariants(self) -> None:
        check_i1(self.h)
        check_i3(self.h)
        check_i5_i27(self.h)
        check_i15(self.h)
        check_i23(self.h)
        check_i29(self.h)
        check_i32(self.h)
        check_i34(self.h)
        stored = {
            str(r["id"]): (str(r["source_binding_id"]), str(r["remote_unit_id"]))
            for r in self.h.rows("SELECT id, source_binding_id, remote_unit_id FROM unit_variant")
        }
        assert stored == {s.id: (s.binding, s.remote) for s in self.shadow.values()}  # I32

    @invariant()
    def one_live_variant_per_binding_team_place(self) -> None:
        """Two live Variants of one binding and Team on a place exist only under a conflict."""
        rows = self.h.rows(
            "SELECT id, source_binding_id, team_id, unit_id "
            "FROM unit_variant WHERE removed_at IS NULL"
        )
        groups: dict[tuple[object, ...], list[str]] = {}
        for r in rows:
            groups.setdefault((r["source_binding_id"], r["team_id"], r["unit_id"]), []).append(
                str(r["id"])
            )
        for members in groups.values():
            if len(members) > 1:
                assert set(members) <= self.in_conflict

    @invariant()
    def oldest_variant_keeps_the_place(self) -> None:  # I4
        for unit_id in {s.unit_id for s in self.shadow.values()}:
            mine = [s for s in self.shadow.values() if s.unit_id == unit_id]
            pool = [s for s in mine if not s.removed] or mine
            assert self.h.catalog.place_owner(unit_id) == min(pool, key=lambda s: s.arrival).id


@pytest.mark.req(
    "ENF-QUAL-006", "EF-BIB-034", "EF-BIB-050", "EF-BIB-035", "EF-BIB-001", "ENF-I18N-005"
)
def test_model_invariants_property_suite() -> None:
    """ENF-QUAL-006: stateful suite on a migrated database (I1-I5, I15, I23, I27, I29, I32, I34)."""
    run_state_machine_as_test(
        ModelInvariants,
        settings=settings(
            max_examples=100, stateful_step_count=25, deadline=None, derandomize=True
        ),
    )


@pytest.mark.req("EF-BIB-034")
@settings(max_examples=25, deadline=None)
@given(
    teams=st.integers(1, 4), chapters=st.integers(1, 8), shuffle=st.randoms(use_true_random=False)
)
def test_n_teams_m_chapters_yield_m_units_nm_variants_m_files(
    teams: int, chapters: int, shuffle: random.Random
) -> None:
    with harness() as h:
        edition = h.edition()
        binding = h.binding(edition)
        feed = [(t, c) for t in range(teams) for c in range(1, chapters + 1)]
        shuffle.shuffle(feed)
        for step, (t, c) in enumerate(feed):
            h.catalog.ingest(
                IncomingUnit(
                    binding=binding,
                    remote_unit_id=f"t{t}-c{c}",
                    raw_number=str(c),
                    seen_at=at(step),
                    team_label=f"Team {t}",
                )
            )
        assert h.scalar("SELECT count(*) FROM unit") == chapters
        assert h.scalar("SELECT count(*) FROM unit_variant") == teams * chapters
        for unit in h.rows("SELECT id FROM unit"):
            h.catalog.elect_preferred(str(unit["id"]))
        assert h.scalar("SELECT count(*) FROM unit_variant WHERE preferred") == chapters  # M files
        check_i3(h)


@pytest.mark.req("EF-BIB-050")
@settings(max_examples=25, deadline=None)
@given(
    seasons=st.lists(st.integers(1, 5), min_size=2, max_size=4),
    pick=st.tuples(st.integers(1, 4), st.integers(1, 5)),
)
def test_season_reset_creates_distinct_units(seasons: list[int], pick: tuple[int, int]) -> None:
    with harness() as h:
        edition = h.edition()
        binding = h.binding(edition)
        step = 0
        for s, count in enumerate(seasons, start=1):
            for c in range(1, count + 1):
                step += 1
                h.catalog.ingest(
                    IncomingUnit(
                        binding=binding,
                        remote_unit_id=f"s{s}c{c}",
                        raw_number=f"S{s} Ch.{c}",
                        seen_at=at(step),
                    )
                )
        assert h.scalar("SELECT count(*) FROM unit") == sum(
            seasons
        )  # numbering restarts: no collision
        assert h.scalar("SELECT count(DISTINCT number_segment) FROM unit") == len(seasons)
        mark = follow_from(parse_number(f"S{pick[0]} Ch.{pick[1]}"))
        wanted = {
            f"S{s} Ch.{c}"
            for s, count in enumerate(seasons, start=1)
            for c in range(1, count + 1)
            if is_beyond(parse_number(f"S{s} Ch.{c}"), mark)
        }
        got = {
            str(r["number_raw"])
            for u in h.catalog.units_beyond(edition.id, mark)
            for r in h.rows("SELECT number_raw FROM unit WHERE id = :i", i=u)
        }
        assert got == wanted
        check_i34(h)
