"""ENF-QUAL-006: the identity invariants of 02 section 3.1 bis, on a real migrated SQLite database.

I1 (places and variants), I2 (N teams x M chapters), I3 (one exported Variant per Unit), I4 (the
oldest Variant keeps the place), I5 and I27 (a Team belongs to one Edition), I15 (no nullable key
column), I23 (canonical languages), I29 (normalizer version and raw value kept), I32 (a
`remote_unit_id` is never rewritten, revisions are append-only), I34 (watermark and place share
the segment).

The second half exercises the host-side invariants on real files and the real export pipeline:
I24 (an `adopt_ro` root is never written, no `rename` between two physical roots) and I37 (no
plan touches a reserved path) on `Fsops`; I28 (nothing replayed, written or scanned while the
database is behind the disk), I33 (no identity in an exported file) and I39 (one generation step
per write session, database first) on a restored library. I42 (an upstream identity survives a
reinstallation of the connected tool) is a property of the Suwayomi identity mapping.
"""

import hashlib
import random
import tempfile
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
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
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.restore import RestoredBehindError
from shelfhold.infrastructure.fsops import (
    CrossRootError,
    PathEscapeError,
    ReadOnlyRootError,
    ReservedPathError,
    RootBlockedError,
)
from shelfhold.infrastructure.instance import WriteSessions
from shelfhold.sdk.refs import UnitInfo
from tests.catalog_env import Harness, at, harness
from tests.fsops_env import Env, make_env
from tests.restore_env import RestoreEnv, restore_env

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


# --- I24 and I37: the fsops operations a destination plan can ask for -----------------------

RESERVED_NAMES = (
    ".shelfhold-mount-id",
    ".shelfhold/lease",
    ".shelfhold/manifest/x.jsonl",
    ".shelfhold/staging/part",
    ".shelfhold/recycle/old.cbz",
    ".shelfhold/tombstones/journal",
    "series/../.shelfhold/lease",
)
PLAIN_NAMES = st.from_regex(r"[a-z]{1,6}(/[a-z]{1,6})?\.cbz", fullmatch=True)


def tree_digest(base: Path, *, reserved_only: bool) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(base).as_posix()
        reserved = rel == ".shelfhold-mount-id" or rel.split("/")[0] == ".shelfhold"
        if path.is_file() and reserved == reserved_only:
            found[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


class FsopsInvariants(RuleBasedStateMachine):
    """I24 and I37 on real directories: three roots under /library (`books` is its own physical
    root), one read-only adoption volume."""

    def __init__(self) -> None:
        super().__init__()
        self.tmp = tempfile.TemporaryDirectory()
        self.env: Env = make_env(Path(self.tmp.name))
        self.roots = {
            "manga": self.env.manga,
            "comics": self.env.comics,
            "books": self.env.books,
            "upstream": self.env.upstream,
        }
        self.physical = {"manga": "library", "comics": "library", "books": "books"}
        self.files: dict[str, bytes] = {}  # absolute path -> content, writable roots only
        self.sentinels = tree_digest(self.env.library, reserved_only=True)
        self.books_reserved = tree_digest(self.env.books, reserved_only=True)

    def teardown(self) -> None:
        self.tmp.cleanup()

    @rule(root=st.sampled_from(["manga", "comics", "books", "upstream"]), name=PLAIN_NAMES,
          data=st.binary(max_size=16))  # fmt: skip
    def write_ordinary(self, root: str, name: str, data: bytes) -> None:
        target = self.roots[root] / name
        if root == "upstream":
            with pytest.raises(ReadOnlyRootError):
                self.env.fsops.write_atomic(str(target), data, parents=True)
            return
        self.env.fsops.write_atomic(str(target), data, parents=True)
        self.files[str(target)] = data

    @rule(
        root=st.sampled_from(["library", "books", "upstream"]),
        name=st.sampled_from(RESERVED_NAMES),
        op=st.sampled_from(["write", "unlink", "mkdir"]),
    )
    def touch_reserved(self, root: str, name: str, op: str) -> None:
        """Reserved names live at the top of a physical root, not in a sub-folder of it."""
        base = {"library": self.env.library, "books": self.env.books}.get(root, self.env.upstream)
        target = str(base / name)
        refused = (ReservedPathError, ReadOnlyRootError, PathEscapeError)
        with pytest.raises(refused):
            if op == "write":
                self.env.fsops.write_atomic(target, b"forged", parents=True)
            elif op == "unlink":
                self.env.fsops.unlink(target)
            else:
                self.env.fsops.mkdir(target)

    @rule(src=st.sampled_from(["manga", "comics", "books", "upstream"]),
          dst=st.sampled_from(["manga", "comics", "books", "upstream"]),
          name=PLAIN_NAMES, reserved=st.sampled_from([None, *RESERVED_NAMES]))  # fmt: skip
    def rename(self, src: str, dst: str, name: str, reserved: str | None) -> None:
        source = self.roots[src] / name
        top = {"manga": self.env.library, "comics": self.env.library, "books": self.env.books}
        target = (
            top.get(dst, self.env.upstream) / reserved
            if reserved
            else self.roots[dst] / ("moved-" + name.replace("/", "-"))
        )
        if src != "upstream" and str(source) not in self.files:
            self.env.fsops.write_atomic(str(source), b"x", parents=True)
            self.files[str(source)] = b"x"
        if source.exists() or src == "upstream":
            before = tree_digest(self.env.tmp, reserved_only=False)
            cross = (
                src == "upstream"
                or dst == "upstream"
                or (self.physical.get(src) != self.physical.get(dst))
            )
            if reserved is not None or cross:
                with pytest.raises(
                    (ReservedPathError, ReadOnlyRootError, CrossRootError, PathEscapeError)
                ):
                    self.env.fsops.rename(str(source), str(target))
                assert tree_digest(self.env.tmp, reserved_only=False) == before
                return
            self.env.fsops.rename(str(source), str(target), replace=True)
            self.files[str(target)] = self.files.pop(str(source))

    @invariant()
    def adopt_ro_root_stays_untouched(self) -> None:  # I24
        assert list(self.env.upstream.rglob("*")) == []

    @invariant()
    def reserved_paths_belong_to_the_host(self) -> None:  # I37
        assert tree_digest(self.env.library, reserved_only=True) == self.sentinels
        assert tree_digest(self.env.books, reserved_only=True) == self.books_reserved

    @invariant()
    def a_file_never_changes_physical_root(self) -> None:  # I24
        on_disk = {
            str(p): p.read_bytes()
            for name in ("manga", "comics", "books")
            for p in self.roots[name].rglob("*.cbz")
            if p.is_file()
        }
        assert on_disk == self.files


@pytest.mark.req("ENF-QUAL-006", "EF-EXP-060", "EF-EXP-001")
def test_i24_i37_fsops_never_writes_adopt_ro_or_reserved_paths() -> None:
    run_state_machine_as_test(
        FsopsInvariants,
        settings=settings(max_examples=30, stateful_step_count=15, deadline=None, derandomize=True),
    )


# --- I28, I33, I39: a real library, exported, backed up, restored behind, re-adopted --------


class RestoreInvariants(RuleBasedStateMachine):
    """Random histories of exports, backups and restores over the real pipeline."""

    def __init__(self) -> None:
        super().__init__()
        self.stack = ExitStack()
        self.env: RestoreEnv = self.stack.enter_context(restore_env(first_chapters=1))
        self.next_chapter = 2
        self.backup: Path | None = None
        self.exported_since_backup = 0
        self.behind = False
        self.frozen_disk: dict[str, str] = {}
        self.intent: tuple[str, Path] | None = None  # (intent id, source it would rename)

    def teardown(self) -> None:
        self.stack.close()

    def generations(self) -> tuple[int, int]:
        with self.env.engine.connect() as connection:
            database = connection.execute(
                sa.text("SELECT generation FROM physical_root WHERE path_entry = :p"),
                {"p": str(self.env.library)},
            ).scalar_one()
        return int(database), int(self.env.sentinel()["generation"])  # type: ignore[call-overload]

    @precondition(lambda self: not self.behind)
    @rule()
    def export_a_chapter(self) -> None:
        env = self.env
        env.add_chapters(self.next_chapter, self.next_chapter)
        self.next_chapter += 1
        before = len(env.managed())
        database, sentinel = self.generations()
        assert database == sentinel  # I39: nothing is ever left half-stamped between sessions
        real_open = WriteSessions.open
        with patch.object(WriteSessions, "open", autospec=True, side_effect=real_open) as spy:
            env.export()
        assert len(env.managed()) == before + 1
        self.exported_since_backup += 1
        moved_to, stamped = self.generations()
        assert moved_to == stamped  # I39: the sentinel follows the database
        assert spy.call_count >= 1
        assert (
            moved_to - database == spy.call_count
        )  # I39: one session, one step, whatever it wrote

    @precondition(lambda self: not self.behind)
    @rule()
    def take_backup(self) -> None:
        self.backup = self.env.backup()
        self.exported_since_backup = 0

    @precondition(lambda self: not self.behind and self.intent is None)
    @rule()
    def leave_an_intent(self) -> None:
        source = sorted(self.env.managed())[0]
        intent = IntentJournal(self.env.engine, self.env.fsops, clock=self.env.ex.clock).record(
            op="rename", src=str(source), dst=str(source.with_name("renamed.cbz")),
            expected_sha256="sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
        )  # fmt: skip
        self.intent = (intent, source)

    @precondition(lambda self: not self.behind and self.backup is not None
                  and self.exported_since_backup > 0)  # fmt: skip
    @rule()
    def restore_the_database(self) -> None:
        assert self.backup is not None
        self.env.restore(self.backup)
        assert self.env.guard().enter_restored_behind().behind
        self.behind = True
        self.frozen_disk = self.env.files()

    @precondition(lambda self: not self.behind)
    @rule()
    def back_up_export_then_restore(self) -> None:
        """The shortest history that puts the disk ahead of the database."""
        self.take_backup()
        self.export_a_chapter()
        self.restore_the_database()

    @precondition(lambda self: self.behind)
    @rule()
    def try_to_act_while_behind(self) -> None:  # I28
        env = self.env
        victim = env.managed()[0]
        with pytest.raises(RootBlockedError):
            env.fsops.write_atomic(str(victim.with_name("new.cbz")), b"x", parents=True)
        with pytest.raises(RootBlockedError):
            env.fsops.unlink(str(victim))
        with pytest.raises(RootBlockedError):
            env.fsops.require_scan_allowed(str(victim))
        with pytest.raises(RestoredBehindError):
            env.export()
        if self.intent is not None:
            report = IntentJournal(env.engine, env.fsops, clock=env.ex.clock).resolve_open()
            assert self.intent[0] in report.stale_from_backup  # never replayed
            assert self.intent[1].exists()
            assert not self.intent[1].with_name("renamed.cbz").exists()
            self.intent = None

    @precondition(lambda self: self.behind)
    @rule()
    def adopt_after_restore(self) -> None:
        adopter = self.env.adopter()
        adopter.adopt(confirm=adopter.preview().fingerprint)
        self.behind = False
        self.backup = None
        self.exported_since_backup = 0
        self.next_chapter = 1 + max(int(n) for n in self.env.chapters) if self.env.chapters else 2
        self.next_chapter += 100  # numbers of chapters that exist on disk only: never reused

    @invariant()
    def disk_is_untouched_while_behind(self) -> None:  # I28
        if self.behind:
            assert self.env.files() == self.frozen_disk

    @invariant()
    def no_identity_in_an_exported_file(self) -> None:  # I33
        with self.env.engine.connect() as connection:
            instance_id = connection.execute(sa.text("SELECT instance_id FROM instance")).scalar()
            ids = {
                str(row[0]).lower()
                for table in ("unit", "unit_variant", "edition", "work", "export_item")
                for row in connection.execute(sa.text(f"SELECT id FROM {table}"))  # noqa: S608
            }
        needles = {str(instance_id).lower(), "shelfhold", "rayonnage", *ids}
        for path in self.env.managed():
            raw = path.read_bytes().lower()
            for needle in needles:
                assert needle.encode() not in raw, (path.name, needle)
            with zipfile.ZipFile(path) as archive:
                assert archive.comment == b""
                assert all(info.comment == b"" and info.extra == b"" for info in archive.infolist())

    @invariant()
    def database_is_never_more_than_one_step_ahead(self) -> None:  # I39
        if not self.behind:
            database, sentinel = self.generations()
            assert database == sentinel


@pytest.mark.req("ENF-QUAL-006", "EF-INT-026", "EF-EXP-063", "EF-EXP-029")
def test_i28_i33_i39_restore_history_keeps_the_disk_and_the_identity() -> None:
    run_state_machine_as_test(
        RestoreInvariants,
        settings=settings(max_examples=10, stateful_step_count=10, deadline=None, derandomize=True),
    )


# --- I42: an upstream identity outlives the connected tool -----------------------------------


@pytest.mark.req("ENF-QUAL-006", "EF-SRC-036", "EF-BIB-050")
@settings(max_examples=12, deadline=None)
@given(reinstalls=st.integers(1, 3), rotate_secret=st.booleans())
def test_i42_remote_unit_id_is_stable_across_reinstalls(
    reinstalls: int, rotate_secret: bool
) -> None:
    """The tool is recreated (every local integer changes) up to three times: the same upstream
    identities come back, the catalog sees no new Variant, and the integers are only handles."""
    from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
    from tests.fakes.suwayomi import BASE_URL
    from tests.unit.test_suwayomi_adapter import alpha, make

    service, fetcher, server = make()
    ref, units = alpha(service, fetcher)
    with harness() as h:
        edition = h.edition()
        binding = h.binding(edition)

        def feed(found: list[UnitInfo], step: int) -> list[IngestOutcome]:
            return [
                h.catalog.ingest(
                    IncomingUnit(
                        binding=binding,
                        remote_unit_id=u.remote_unit_id,
                        raw_number=u.number,
                        seen_at=at(step),
                        source_order=u.source_order,
                    )
                ).outcome
                for u in found
            ]

        assert all(o is IngestOutcome.NEW_VARIANT for o in feed(units, 0))
        stored = h.scalar("SELECT count(*) FROM unit_variant")
        handles = {u.remote_unit_id: u.memo["fetch_handle"] for u in units}
        for step in range(1, reinstalls + 1):
            server.reinstall()
            fresh = SuwayomiService(BASE_URL)  # a new client object: nothing carried over
            ref_after, found = alpha(fresh, fetcher)
            assert (ref_after.source_key, ref_after.remote_id) == (ref.source_key, ref.remote_id)
            assert {u.remote_unit_id for u in found} == set(handles)
            assert all(o is IngestOutcome.UNCHANGED for o in feed(found, step))
            assert h.scalar("SELECT count(*) FROM unit_variant") == stored
            for u in found:  # the local integer is a cache, never part of the identity
                assert str(u.memo["fetch_handle"]) not in u.remote_unit_id.split(":", 2)[-1]
