"""Local search (M3 L17, EF-BIB-010, EF-BIB-022): FTS5, trigram, `LIKE` under three CJK characters,
contributor names, a projection that `rebuild` reproduces, the article-free sort, and the attacks
on the query syntax. The parental filter is at the query (L19 and L09 build the visibility)."""

import json
import sqlite3
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from shelfhold.catalog.domain.sort_key import sort_key
from shelfhold.catalog.public import (
    index_work,
    rebuild_search_index,
    search_capability,
    search_works,
    sync_search_index,
)
from shelfhold.config import Settings
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.db import create_db_engine
from tests.auth_env import PASSWORD, AuthEnv, auth_env, db_rows, run_cli
from tests.catalog_env import Harness, harness
from tests.search_env import alt_title, credit, index_rows, rate


@pytest.fixture
def h() -> Iterator[Harness]:
    with harness() as h:
        yield h


def _ids(h: Harness, query: str, **kwargs: object) -> list[str]:
    return [hit.work_id for hit in search_works(h.connection, query, **kwargs)]  # type: ignore[arg-type]


def _library(h: Harness) -> dict[str, str]:
    works = {
        "aot": h.work(title="進撃の巨人"),
        "lm": h.work(title="Les Misérables"),
        "ph": h.work(title="Pluto"),
    }
    sync_search_index(h.connection)
    return works


@pytest.mark.req("EF-BIB-010")
def test_search_cjk_trigram(h: Harness) -> None:
    works = _library(h)
    assert _ids(h, "撃の巨") == [works["aot"]]  # the middle of a title: the trigram table
    assert _ids(h, "の巨人") == [works["aot"]]
    assert _ids(h, "巨人の星") == []
    caps = search_capability(h.connection)
    assert caps.mode == "fts5" and caps.trigram and not caps.degraded


@pytest.mark.req("EF-BIB-010")
def test_search_diacritics_folded(h: Harness) -> None:
    works = _library(h)
    for query in ("miserables", "MISÉRABLES", "misérables", "misér", "les miser"):
        assert _ids(h, query) == [works["lm"]], query
    assert _ids(h, "pluto") == [works["ph"]]
    assert _ids(h, "miserables zzz") == []  # every term must be found


@pytest.mark.req("EF-BIB-010")
def test_like_fallback_under_3_cjk_chars(h: Harness) -> None:
    works = _library(h)
    assert _ids(h, "進撃") == [works["aot"]]  # two characters: no trigram, `LIKE`
    assert _ids(h, "巨") == [works["aot"]]
    # the wildcards of LIKE are text, not wildcards
    assert _ids(h, "巨%") == []
    assert _ids(h, "巨_") == []
    assert _ids(h, "進%") == []


@pytest.mark.req("EF-BIB-010")
def test_like_fallback_without_fts5_is_reported(h: Harness) -> None:
    """D7: no FTS tables (SQLite without FTS5 or without trigram): `LIKE`, never a failure."""
    works = _library(h)
    h.connection.execute(sa.text("DROP TABLE search_trigram"))
    caps = search_capability(h.connection)
    assert caps.mode == "fts5" and not caps.trigram and caps.degraded
    assert _ids(h, "撃の巨") == [works["aot"]]  # the trigram is gone: still found, by LIKE
    h.connection.execute(sa.text("DROP TABLE search_fts"))
    caps = search_capability(h.connection)
    assert caps.mode == "like" and caps.degraded
    assert _ids(h, "Les Misér") == [works["lm"]]
    assert _ids(h, "進撃") == [works["aot"]]
    assert _ids(h, "pluto") == [works["ph"]]


@pytest.mark.req("EF-BIB-010")
def test_status_reports_like_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    from shelfhold.infrastructure.migrate import migrate

    migrate(settings)
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "status", "--json") == 0
    assert "search_like_fallback" not in capsys.readouterr().out
    connection = sqlite3.connect(settings.database_path)
    try:
        connection.execute("DROP TABLE search_fts")
        connection.execute("DROP TABLE search_trigram")
        connection.commit()
    finally:
        connection.close()
    assert run_cli(monkeypatch, settings, "status", "--json") == 0
    codes = [w["code"] for w in json.loads(capsys.readouterr().out)["warnings"]]
    assert "search_like_fallback" in codes


@pytest.mark.req("EF-BIB-010")
def test_search_index_rebuilt_includes_contributor_names(h: Harness) -> None:
    work = h.work(title="Shingeki")
    other = h.work(title="Unrelated")
    credit(h, work, ("Hajime Isayama", "en", "romanized"), ("諫山創", "ja", "native"))
    sync_search_index(h.connection)
    assert _ids(h, "諫山創") == [work]
    assert _ids(h, "isayama") == [work]
    assert _ids(h, "Isayama Hajime") == [work]
    before = index_rows(h)
    h.connection.execute(sa.text("DROP TABLE search_fts"))
    h.connection.execute(sa.text("DROP TABLE search_trigram"))  # the index is deleted
    assert rebuild_search_index(h.connection) == 2  # and made again from the database
    assert index_rows(h) == before
    assert _ids(h, "諫山創") == [work] and other not in _ids(h, "isayama")


@pytest.mark.req("EF-BIB-010")
def test_rebuild_equals_incremental(h: Harness) -> None:
    a, b, c = h.work(title="Alpha"), h.work(title="Béta"), h.work(title="進撃の巨人")
    credit(h, a, ("Ann Author", "en", "display"))
    alt_title(h, c, "Attack on Titan", "en")
    for work in (a, b, c):
        index_work(h.connection, work)  # one by one, as the writes come
    incremental = index_rows(h)
    # a later change, picked up by the incremental pass
    credit(h, b, ("Bob 作者", "und", "display"))
    h.connection.execute(sa.text("DELETE FROM work WHERE id = :i"), {"i": a})
    assert sync_search_index(h.connection) == 2  # b changed, a is gone
    incremental = index_rows(h)
    assert rebuild_search_index(h.connection) == 2
    assert index_rows(h) == incremental
    assert all(row[2] != a for row in incremental)


@pytest.mark.req("EF-BIB-010")
def test_index_never_holds_unit_coverage(h: Harness) -> None:
    columns = [
        r[1] for r in h.connection.execute(sa.text("PRAGMA table_info(search_fts)")).fetchall()
    ]
    assert columns == ["entity_type", "entity_id", "digest", "title", "names"]
    from pathlib import Path

    import shelfhold.catalog.application.search as package

    for source in Path(package.__file__ or "").parent.glob("*.py"):
        assert "unit_coverage" not in source.read_text(encoding="utf-8")


@pytest.mark.req("EF-BIB-022")
def test_sort_title_ignores_french_and_english_articles(h: Harness) -> None:
    assert sort_key("Le Petit Prince", "fr") == "petit prince"
    assert sort_key("La Peste", "fr") == "peste"
    assert sort_key("Les Misérables", "fr") == "misérables"
    assert sort_key("L'Étranger", "fr") == "étranger"
    assert sort_key("L\u2019Étranger", "fr-CA") == "étranger"
    assert sort_key("The Hobbit", "en") == "hobbit"
    assert (
        sort_key("The Hobbit", "fr") == "the hobbit"
    )  # the language of the Edition, not of the UI
    one, two, three = (h.work(title=t) for t in ("Le Zèbre", "The Hobbit", "L'Alchimiste"))
    for work, lang in ((one, "fr"), (two, "en"), (three, "fr")):
        h.edition(work, lang=lang)
        credit(h, work, ("Auteur Partage", "und", "display"))
    sync_search_index(h.connection)
    assert _ids(h, "partage") == [three, two, one]  # Alchimiste, Hobbit, Zèbre


@pytest.mark.req("EF-BIB-010")
def test_search_filters_child_at_query_not_index(h: Harness) -> None:
    kids, teen, adult, unrated = (h.work(title=f"Dragon {n}") for n in "ABCD")
    rate(h, kids, 6)
    rate(h, teen, 16)
    rate(h, adult, 18, adult=True)
    sync_search_index(h.connection)
    assert len(index_rows(h)) == 8  # all four, in both tables: the index holds no filter
    assert set(_ids(h, "dragon")) == {kids, teen, adult, unrated}
    assert _ids(h, "dragon", max_age=8) == [kids]  # unrated and above the ceiling: not there
    assert set(_ids(h, "dragon", max_age=16)) == {kids, teen}
    assert _ids(h, "dragon", max_age=0) == []


ATTACKS = [
    '"', '""', '"unterminated', "*", "a*", "NEAR", "NEAR(a b)", "NEAR(a b, 2)", "a OR b", "OR",
    "a AND", "NOT a", "a NOT b", "-", "-secret", "- secret", "^secret", "title:secret",
    "names:secret", "{title names}: secret", "{names}:secret", "entity_id:x", "digest:x",
    "search_fts:x", "(", ")", "(secret", "secret)", "(a OR b) AND c", "a:b:c", "+", "\\", "%", "_",
    "' OR 1=1 --", "'; DROP TABLE search_fts; --", "\x00", "a\x00b", "''", "secret" * 200,
    "a b c d e f g h i j k l m n o p q r s t u v w x y z", "進", "進_", "進%", "a%b", ":", "::",
    "title : secret", "NEAR/2", "CJK 進撃 OR 巨人", "‮", "*:*", "title:*",
]  # fmt: skip


@pytest.mark.req("EF-BIB-010")
@pytest.mark.parametrize("attack", ATTACKS)
def test_search_syntax_injection_is_text(h: Harness, attack: str) -> None:
    """No FTS5 operator, column filter or wildcard survives: the query is words, and the answer is
    the works that hold those words, never an error, never a column that was not meant."""
    hidden = h.work(title="Public Title")
    credit(h, hidden, ("secret", "en", "display"))
    sync_search_index(h.connection)
    # the term that every attack above hides behind is "secret", present in the names column only
    found = _ids(h, attack)
    assert isinstance(found, list)
    assert len(found) <= 1
    for forbidden in ("entity_id:", "digest:", "search_fts:"):
        if attack.startswith(forbidden):
            assert found == []
    assert _ids(h, "*") == []  # a star is not "everything"
    assert _ids(h, "NEAR(public secret)") == []  # NEAR is a word here, not an operator
    assert _ids(h, "public OR secret") == []  # OR is a word here
    assert _ids(h, "public -secret") == [hidden]  # a minus is not a negation: a word like any


@pytest.mark.req("EF-BIB-010")
def test_search_column_targeting_cannot_reach_other_columns(h: Harness) -> None:
    work = h.work(title="Ordinary")
    sync_search_index(h.connection)
    assert _ids(h, f"entity_id:{work}") == []
    assert _ids(h, work) == []  # an id is not a searchable word
    digest = h.connection.execute(sa.text("SELECT digest FROM search_fts")).scalar_one()
    assert _ids(h, digest) == [] and _ids(h, f"digest:{digest}") == []


# -- the HTTP route: admin only until L09 makes the visibility barrier ---------------------------


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


def _seed_users(api: AuthEnv) -> None:
    (admin_hash,) = db_rows(api.settings, "SELECT password_hash FROM user WHERE role = 'admin'")[0]
    connection = sqlite3.connect(api.settings.database_path)
    try:
        connection.execute(
            "INSERT INTO user (id, username, password_hash, role, can_rate) "
            "VALUES ('01J0000000000000000000MEMB', 'member', ?, 'member', 0)",
            (admin_hash,),
        )
        connection.execute(
            "INSERT INTO user (id, username, password_hash, role, can_rate, max_age_rating) "
            "VALUES ('01J0000000000000000000KKKK', 'kid', ?, 'child', 0, 6)",
            (admin_hash,),
        )
        connection.execute(
            "INSERT INTO work (id, kind, title, sort_title) "
            "VALUES ('01J0000000000000000000WRKK', 'manga', 'Quiet Gardens', 'quiet gardens')"
        )
        connection.commit()
    finally:
        connection.close()


def _key_of(api: AuthEnv, username: str) -> str:
    engine = create_db_engine(api.settings.database_path)
    try:
        return (
            local_auth(engine, api.settings)
            .create_api_key(name="k", username=username, read_only=True)
            .value
        )
    finally:
        engine.dispose()


@pytest.mark.req("EF-BIB-010")
def test_search_api_is_admin_only_until_the_visibility_barrier(
    monkeypatch: pytest.MonkeyPatch, api: AuthEnv
) -> None:
    _seed_users(api)
    assert run_cli(monkeypatch, api.settings, "search", "rebuild") == 0
    assert api.login().status_code == 200
    ok = api.client.get("/api/v1/search", params={"q": "quiet"})
    assert ok.status_code == 200
    assert [hit["title"] for hit in ok.json()["items"]] == ["Quiet Gardens"]
    for username in ("member", "kid"):
        client = api.other_client("10.0.0.20")
        assert api.login(username, PASSWORD, client=client).status_code == 200
        denied = client.get("/api/v1/search", params={"q": "quiet"})
        assert denied.status_code == 403, username
        assert "Quiet" not in denied.text
        key = _key_of(api, username)
        by_key = api.other_client("10.0.0.21").get(
            "/api/v1/search", params={"q": "quiet"}, headers={"Authorization": f"Bearer {key}"}
        )
        assert by_key.status_code == 403 and "Quiet" not in by_key.text
    assert api.other_client("10.0.0.22").get(
        "/api/v1/search", params={"q": "quiet"}
    ).status_code in (
        401,
        403,
    )


@pytest.mark.req("EF-BIB-010")
def test_search_api_attacks_never_500(monkeypatch: pytest.MonkeyPatch, api: AuthEnv) -> None:
    _seed_users(api)
    assert run_cli(monkeypatch, api.settings, "search", "rebuild") == 0
    assert api.login().status_code == 200
    for attack in ATTACKS:
        response = api.client.get("/api/v1/search", params={"q": attack})
        assert response.status_code in (200, 400, 422), (attack, response.text)
        assert "Traceback" not in response.text and "fts5" not in response.text.lower()


@pytest.mark.req("EF-BIB-010")
def test_search_rebuild_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    _seed_users(api)
    capsys.readouterr()
    assert run_cli(monkeypatch, api.settings, "search", "rebuild", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {"indexed": 1, "mode": "fts5", "trigram": True}
    assert run_cli(monkeypatch, api.settings, "search", "rebuild") == 0
