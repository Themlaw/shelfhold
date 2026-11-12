"""M2 L15b: "mort pour la France" is recorded (`contributor.died_for_france`, NULL = unknown), read
by the rights review and set by the reserved action `contributor set-died-for-france`; an unknown
value warns on the cautious side. The warning never blocks anything (EF-SRC-024, EF-BIB-046)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import RightsService
from shelfhold.config import Settings
from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    Credential,
    CredentialNotAllowed,
    Principal,
    ReauthRequired,
    authorize_reserved,
)
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, identity
from shelfhold.interfaces.local_cli.ops import OPS_METHODS
from shelfhold.ops.contributors import ContributorOps
from shelfhold.ops.errors import CodedRefusal, UnknownSubjectError
from tests.auth_env import run_cli
from tests.catalog_env import Harness, harness
from tests.l50_env import seeded
from tests.integration.test_rights import _book, _credit, _instance, _person

pytestmark = pytest.mark.req("EF-SRC-024", "EF-BIB-046")

TODAY = date(2026, 11, 11)


def _marked(h: Harness, contributor_id: str, value: bool | None) -> None:
    h.connection.execute(
        sa.update(catalog.contributor)
        .where(catalog.contributor.c.id == contributor_id)
        .values(died_for_france=value)
    )


def _review(  # type: ignore[no-untyped-def]
    h: Harness, death_year: int, value: bool | None, published: str = "1939", today: date = TODAY
):
    _instance(h, "FR")
    edition = _book(h, first_published=published)
    author = _person(h, "Auteur", death_year)
    _credit(h, author, edition.id, edition.work_id, "aut")
    _marked(h, author, value)
    return RightsService(h.connection).review(edition.id, today=today)


def test_an_author_marked_died_for_france_is_still_protected_in_france_with_a_warning() -> None:
    with harness() as h:
        report = _review(h, 1944, True)
        assert report.verdict == "may_be_protected"
        assert report.warnings[0].protected_until.year >= 2044


def test_an_author_marked_no_who_died_in_1960_keeps_the_existing_behaviour() -> None:
    with harness() as h:
        report = _review(h, 1960, False, published="1950")
        assert report.verdict == "may_be_protected"  # life + 70 = 2030
        assert report.warnings[0].protected_until == date(2030, 12, 31)


def test_marked_no_beats_the_cautious_reading_for_a_war_death() -> None:
    with harness() as h:
        no = _review(h, 1916, False, published="1910", today=date(2015, 6, 1))
        assert no.verdict == "no_warning"  # 1916 + 70 + 6 y 152 d: over in 1992


def test_an_unknown_author_who_died_in_1916_triggers_the_warning() -> None:
    with harness() as h:
        report = _review(h, 1916, None, published="1910", today=date(2015, 6, 1))
        assert report.verdict == "may_be_protected"  # the 30 years could run until 2022
        assert report.warnings[0].protected_until >= TODAY


def test_an_unknown_author_who_died_in_peace_does_not_warn() -> None:
    with harness() as h:
        assert _review(h, 1930, None, published="1920").verdict == "no_warning"


def test_the_warning_for_an_unknown_value_is_an_event_and_never_a_block() -> None:
    with harness() as h:
        _instance(h, "FR")
        edition = _book(h, first_published="1910")
        author = _person(h, "Auteur", 1916)
        _credit(h, author, edition.id, edition.work_id, "aut")
        rights = RightsService(h.connection)
        report = rights.review(edition.id, today=date(2015, 6, 1))
        assert report.verdict == "may_be_protected"
        again = h.edition(edition.work_id, kind="novel", lang="en", media="text")
        assert again.id != edition.id  # the next Edition is accepted all the same


def test_the_default_of_the_column_is_unknown() -> None:
    with harness() as h:
        who = _person(h, "Auteur", 1916)
        row = h.rows("SELECT died_for_france FROM contributor WHERE id=:i", i=who)[0]
        assert row["died_for_france"] is None


# -- the command ---------------------------------------------------------------------------------
def _contributor(settings: Settings, tmp_path: Path):  # type: ignore[no-untyped-def]
    return seeded(settings, tmp_path / "inst")


def _value(env, who: str):  # type: ignore[no-untyped-def]
    row = [r for r in env.rows(catalog.contributor) if r.id == who][0]
    return row.died_for_france


def _make(env) -> str:  # type: ignore[no-untyped-def]
    who = new_ulid()
    with env.inst.engine.begin() as connection:
        connection.execute(
            sa.insert(catalog.contributor).values(id=who, kind="person", primary_name="Auteur")
        )
    return who


@pytest.mark.parametrize(("flag", "expected"), [("--yes", True), ("--no", False)])
def test_set_died_for_france_writes_the_value_and_audits_it_without_a_name(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, flag: str,
    expected: bool,
) -> None:
    with _contributor(settings, tmp_path) as env:
        who = _make(env)
        assert run_cli(monkeypatch, settings, "contributor", "set-died-for-france", who, flag) == 0
        assert _value(env, who) is expected
        audit = [r for r in env.rows(identity.audit) if r.action == "contributor.set_died_for_france"]
        assert len(audit) == 1
        assert who in audit[0].target and "Auteur" not in audit[0].target  # an id, never a name


def test_set_died_for_france_unknown_resets_to_null_and_is_audited(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with _contributor(settings, tmp_path) as env:
        who = _make(env)
        assert run_cli(monkeypatch, settings, "contributor", "set-died-for-france", who, "--yes") == 0
        assert run_cli(monkeypatch, settings, "contributor", "set-died-for-france", who, "--unknown") == 0
        assert _value(env, who) is None
        actions = [r.action for r in env.rows(identity.audit)]
        assert actions.count("contributor.set_died_for_france") == 2


def test_set_died_for_france_wants_exactly_one_answer_and_a_known_contributor(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with _contributor(settings, tmp_path) as env:
        who = _make(env)
        for flags in ((), ("--yes", "--no"), ("--yes", "--unknown")):
            code = run_cli(monkeypatch, settings, "contributor", "set-died-for-france", who, *flags)
            assert code != 0
        assert _value(env, who) is None
        assert run_cli(monkeypatch, settings, "contributor", "set-died-for-france",
                       new_ulid(), "--yes") != 0  # no such contributor
        assert run_cli(monkeypatch, settings, "contributor", "set-died-for-france",
                       "not-an-id", "--yes") != 0
        assert [r for r in env.rows(identity.audit)
                if r.action == "contributor.set_died_for_france"] == []  # a refusal leaves no trace


def test_set_died_for_france_is_reserved_never_for_a_key_or_a_stale_session(
    settings: Settings, tmp_path: Path
) -> None:
    assert OPS_METHODS["ops.contributor_set_died_for_france"][1] == "contributor.set_died_for_france"
    action = RESERVED_ACTIONS["contributor.set_died_for_france"]
    key = Principal("u", "admin", "admin", Credential.API_KEY, key_id="k")
    stale = Principal("u", "admin", "admin", Credential.SESSION, session_id="s")
    now = datetime(2026, 11, 11, tzinfo=UTC)
    assert isinstance(authorize_reserved(key, action, now=now), CredentialNotAllowed)
    assert isinstance(authorize_reserved(stale, action, now=now), ReauthRequired)
    with _contributor(settings, tmp_path) as env:
        who = _make(env)
        ops = ContributorOps(env.inst.comp)
        for who_asks in (key, stale):
            with pytest.raises(CodedRefusal):
                ops.set_died_for_france(who, True, principal=who_asks)
        assert _value(env, who) is None
        assert [r for r in env.rows(identity.audit)
                if r.action == "contributor.set_died_for_france"] == []
        with pytest.raises(UnknownSubjectError):
            ops.set_died_for_france(new_ulid(), True, principal=Principal(
                "1000", "uid:1000", "admin", Credential.LOCAL_CLI))
