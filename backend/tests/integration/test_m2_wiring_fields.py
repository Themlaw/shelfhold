"""M2 wiring (L12, L15): the field locks, the renumbering of an external identifier and the sorted
list of Editions, on the real doors. `shelfhold meta lock|unlock`, `shelfhold edition renumber-id`
and `shelfhold edition list --sort title` run over a real instance; an enrichment that comes
after a lock is the production `meta enrich` against a fake OpenLibrary, so the attack goes
through the same door a provider does."""

from __future__ import annotations

from pathlib import Path

import pytest

from shelfhold.catalog.application.external_ids import add_external_id, external_ids_of
from shelfhold.catalog.domain.external_ids import ExternalId
from shelfhold.config import Settings
from shelfhold.infrastructure.schema import catalog
from tests.auth_env import run_cli
from tests.fakes.openlibrary.server import ISBN, FakeOpenLibrary
from tests.integration.test_cli_meta_enrich import field, wire
from tests.integration.test_schema_catalog import add_edition
from tests.l50_env import seeded
from tests.l54_env import bench54


def _row(b: object, edition: str, name: str, lang: str = "und") -> dict[str, object] | None:
    rows = b.rows(  # type: ignore[attr-defined]
        "SELECT value, provider, provenance, locked FROM metadata_field "
        "WHERE entity_id = :e AND field = :f AND lang = :l",
        e=edition, f=name, l=lang,
    )  # fmt: skip
    return rows[0] if rows else None


@pytest.mark.req("EF-BIB-008")
def test_a_locked_title_survives_the_enrichment_that_follows(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        edition = wire(b, FakeOpenLibrary(), monkeypatch)
        assert b.cli("meta", "lock", edition, "title", "Mon titre") == 0
        stored = _row(b, edition, "title")
        assert stored == {
            "value": '"Mon titre"', "provider": "admin", "provenance": "manual", "locked": 1,
        }  # fmt: skip
        capsys.readouterr()
        assert b.cli("meta", "enrich", ISBN) == 0
        assert "title" in capsys.readouterr().out  # told as kept, not as written
        again = _row(b, edition, "title")
        assert again == stored  # value, provider, provenance and lock: untouched
        assert field(b, edition, "publisher") is not None  # the free fields were filled


@pytest.mark.req("EF-BIB-008")
def test_lock_without_a_value_locks_what_a_provider_already_gave(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        fake = FakeOpenLibrary()
        edition = wire(b, fake, monkeypatch)
        assert b.cli("meta", "enrich", ISBN) == 0
        given = _row(b, edition, "title")
        assert given is not None and given["locked"] == 0
        assert b.cli("meta", "lock", edition, "title") == 0
        locked = _row(b, edition, "title")
        assert locked is not None and locked["locked"] == 1
        assert (locked["value"], locked["provider"]) == (given["value"], given["provider"])
        fake.books[ISBN] = {**fake.books[ISBN], "title": "A newer title"}
        assert b.cli("meta", "enrich", ISBN) == 0
        assert _row(b, edition, "title") == locked  # the lock outlives the next proposal


@pytest.mark.req("EF-BIB-008")
def test_unlock_keeps_the_value_and_a_provider_may_not_replace_a_hand_typed_one(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        edition = wire(b, FakeOpenLibrary(), monkeypatch)
        assert b.cli("meta", "lock", edition, "publisher", "Mon éditeur") == 0
        assert b.cli("meta", "unlock", edition, "publisher") == 0
        unlocked = _row(b, edition, "publisher")
        assert unlocked is not None
        assert (unlocked["value"], unlocked["locked"], unlocked["provenance"]) == (
            '"Mon éditeur"', 0, "manual",
        )  # fmt: skip
        assert b.cli("meta", "enrich", ISBN) == 0
        assert _row(b, edition, "publisher") == unlocked  # `manual` still protects it
        assert b.cli("meta", "unlock", edition, "never_set") != 0  # nothing there: refused


@pytest.mark.req("EF-BIB-008")
@pytest.mark.parametrize(
    "field_name",
    ["", " ", "../etc", "Title", "ti tle", "ti\ntle", "x" * 65, "titre\x00"],
)
def test_lock_refuses_a_hostile_field_name_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
    field_name: str,
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        edition = env.new_series("Un roman").edition_id
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "meta", "lock", edition, field_name, "x") != 0
        assert env.rows(catalog.metadata_field) == []
        assert "Refusé" in capsys.readouterr().err


@pytest.mark.req("EF-BIB-008")
def test_lock_refuses_an_absurd_value_an_unknown_edition_and_a_bad_language(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        edition = env.new_series("Un roman").edition_id
        run = lambda *a: run_cli(monkeypatch, settings, "meta", "lock", *a)  # noqa: E731
        assert run(edition, "title", "x" * 5000) != 0
        assert run("01JZZZZZZZZZZZZZZZZZZZZZZZ", "title", "x") != 0
        assert run("not-an-id", "title", "x") != 0
        assert run(edition, "title", "x", "--lang", "!!") != 0
        assert run(edition, "title", "x", "--on", "series") != 0
        assert env.rows(catalog.metadata_field) == []
        assert run(edition, "title", "Bon titre", "--lang", "fr", "--on", "work") == 0
        (row,) = env.rows(catalog.metadata_field)
        assert (row.entity_type, row.field, row.lang, row.locked) == ("work", "title", "fr", 1)


@pytest.mark.req("EF-BIB-003")
def test_renumbering_adds_the_new_value_and_supersedes_the_old_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        edition = env.new_series("Un roman").edition_id
        with env.inst.engine.begin() as connection:
            add_external_id(connection, edition, ExternalId("edition", "anilist", "30013"),
                            origin="provider")  # fmt: skip
        args = ("edition", "renumber-id", edition, "anilist", "30013", "ab12cd")
        assert run_cli(monkeypatch, settings, *args) == 0
        assert run_cli(monkeypatch, settings, *args) == 0  # the same again: nothing changes
        with env.inst.engine.connect() as connection:
            stored = {
                (s.external.value, s.state, s.origin)
                for s in external_ids_of(connection, "edition", edition)
            }
        assert stored == {("30013", "superseded", "legacy"), ("ab12cd", "current", "provider")}
        capsys.readouterr()
        run_cli(monkeypatch, settings, "edition", "renumber-id", edition, "anilist", "ab12cd",
                "zz99")  # fmt: skip
        with env.inst.engine.connect() as connection:
            assert len(external_ids_of(connection, "edition", edition)) == 3  # appended, never lost


@pytest.mark.req("EF-BIB-003")
def test_renumbering_never_steals_an_identifier_and_never_invents_the_old_one(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        one = env.new_series("Un roman").edition_id
        with env.inst.engine.begin() as connection:
            other = add_edition(connection, env.new_series("Autre").work_id, lang="fr",
                                publisher="Beta")  # fmt: skip
            add_external_id(connection, one, ExternalId("edition", "anilist", "1"), origin="manual")
            add_external_id(connection, other, ExternalId("edition", "anilist", "2"),
                            origin="manual")  # fmt: skip
        run = lambda *a: run_cli(monkeypatch, settings, "edition", "renumber-id", *a)  # noqa: E731
        assert run(one, "anilist", "1", "2") != 0  # the new value names another Edition
        assert run(one, "anilist", "99", "100") != 0  # the old one is not this Edition's
        assert run(other, "anilist", "1", "5") != 0  # ... nor the other's
        assert run(one, "ANI LIST", "1", "7") != 0  # not a provider key
        assert run(one, "anilist", "1", " ") != 0  # an empty new value
        with env.inst.engine.connect() as connection:
            assert [(s.external.value, s.state) for s in external_ids_of(connection, "edition", one)] == [("1", "current")]
            assert [(s.external.value, s.state) for s in external_ids_of(connection, "edition", other)] == [("2", "current")]


@pytest.mark.req("ENF-I18N-003")
def test_edition_list_sorts_by_the_title_without_the_article_of_the_edition_language(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        for title, lang in (
            ("The Hobbit", "en"), ("Le Petit Prince", "fr"), ("Alpha", "en"), ("An Apple", "en"),
            ("Le Petit Prince", "en"),  # an English Edition keeps the French title as it is
            ("A", "en"),
        ):
            env.new_series(title, lang=lang)
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "edition", "list", "--sort", "title") == 0
        lines = capsys.readouterr().out.strip().splitlines()
        shown = [(ln.split("\t")[1], ln.split("\t")[2]) for ln in lines]
        assert shown == [
            ("en", "A"), ("en", "Alpha"), ("en", "An Apple"), ("en", "The Hobbit"),
            ("en", "Le Petit Prince"),  # sorted under L: no French article dropped in English
            ("fr", "Le Petit Prince"),  # sorted under P
        ]  # fmt: skip
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "edition", "list", "--sort", "title",
                       "--limit", "2") == 0  # fmt: skip
        assert len(capsys.readouterr().out.strip().splitlines()) == 2
        assert run_cli(monkeypatch, settings, "edition", "list", "--sort", "price") != 0
        assert run_cli(monkeypatch, settings, "edition", "list", "--limit", "0") != 0
        assert run_cli(monkeypatch, settings, "edition", "list", "--limit", "100000") != 0
