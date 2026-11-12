"""Review PoC of #207 (M3 L19, contradicteur): what a child still gets from the family search.

Each test is red on the branch as it stands: it states what must hold and shows that it does not.
"""

from __future__ import annotations

import sqlite3

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.ids import new_ulid
from tests.auth_env import PASSWORD, db_rows, run_cli
from tests.federated_env import CHILD_ID, FakeSource, hit, instance
from tests.integration.test_family_search import kid_client, mark_family, open_door, set_profile
from tests.visibility_env import HIDDEN_TITLES, add_profile, seed_catalogue

SEARCH = "/api/v1/search"


def bind(database: str, source_key: str, remote_id: str, work_id: str, edition_id: str) -> None:
    """The catalogue already holds this remote result: an Edition bound to it (`source_binding`)."""
    connection = sqlite3.connect(database)
    try:
        item_id = new_ulid()
        connection.execute(
            "INSERT INTO source_item (id, plugin_id, source_key, remote_id) VALUES (?, ?, ?, ?)",
            (item_id, source_key, source_key, remote_id),
        )
        connection.execute(
            "INSERT INTO source_binding (id, work_id, edition_id, source_item_id) "
            "VALUES (?, ?, ?, ?)", (new_ulid(), work_id, edition_id, item_id),
        )  # fmt: skip
        connection.commit()
    finally:
        connection.close()


@pytest.mark.req("EF-USR-014", "EF-USR-006")
def test_review_poc_a_work_rated_above_the_ceiling_comes_back_through_a_family_source(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """The admin rated this Work 16+ (above the child's 10): L09 makes it exactly like a Work that
    does not exist. The family source that holds it names it as an "unrated" result, and the
    family search shows its title with "request": the rating the catalogue knows is ignored.
    EF-USR-014 only allows the reduced form for a result that is *not* rated."""
    above = HIDDEN_TITLES["above"]
    source = FakeSource("source-a", name="Source A", pages=[[hit("r-above", above)]])
    with instance(monkeypatch, settings, source) as api:
        seed = seed_catalogue(api)
        bind(
            str(api.settings.database_path), "source-a", "r-above",
            seed.works["above"], seed.editions["above"],
        )  # fmt: skip
        mark_family(api, "source-a")
        add_profile(api, "kid10", "child", "10.0.0.44", max_age_rating=10)
        (kid_id,) = db_rows(api.settings, "SELECT id FROM user WHERE username = 'kid10'")[0]
        set_profile(api, str(kid_id), hide_unrated_remote=0)
        assert run_cli(monkeypatch, api.settings, "search", "rebuild") == 0
        client = api.other_client("10.0.0.45")
        assert api.login("kid10", PASSWORD, client=client).status_code == 200
        local = client.get(SEARCH, params={"q": "Harbor"}).json()
        assert local["items"] == []  # L09: hidden from her catalogue ...
        answer = client.get(SEARCH, params={"scope": "all", "q": "Harbor"})
        assert answer.status_code == 200, answer.text
        assert above not in answer.text  # ... and it must not come back by the remote half
        seed.engine.dispose()


@pytest.mark.req("EF-USR-014")
def test_review_poc_a_source_that_declares_itself_nsfw_is_searched_for_a_child(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """The manifest says `content_warning = "nsfw"`: the source itself says it is not for
    children. One wrong tick of the admin (`family_safe`) and the child searches it, with only the
    word list between her and its titles. A self-declared nsfw (or mixed) source must never be a
    family source, whatever the row says."""
    source = FakeSource("source-a", name="Source A", pages=[[hit("n1", "Night Tale")]])
    source.manifest = source.manifest.model_copy(update={"content_warning": "nsfw"})
    with instance(monkeypatch, settings, source) as api:
        open_door(api)
        mark_family(api, "source-a")
        answer = kid_client(api).get(SEARCH, params={"scope": "all", "q": "tale"})
        assert answer.status_code == 200, answer.text
        assert source.calls == [], "a self-declared nsfw source was asked for a child"
        assert "Night Tale" not in answer.text


@pytest.mark.req("EF-USR-018")
def test_review_poc_flagged_results_leave_a_cursor_that_nothing_found_does_not(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """Four pages of flagged results: the first answer is no item and a `next_cursor`; a search
    that found nothing is no item and no cursor. The child tells "filtered" from "nothing", and
    the signal is not capped: each hop of the cursor proves three more pages of flagged results
    (and costs three source calls: a timing signal too)."""
    flagged = FakeSource(
        "source-a", name="Source A",
        pages=[
            [hit(f"f{n}", f"Erotica Tale {n}", memo={"subjects": ["Erotica"]})] for n in range(7)
        ]
        + [[]],
    )  # fmt: skip
    empty = FakeSource("source-b", name="Source B", pages=[[]])
    with instance(monkeypatch, settings, flagged, empty) as api:
        mark_family(api, "source-a")
        set_profile(api, CHILD_ID, hide_unrated_remote=0)
        kid = kid_client(api)
        filtered = kid.get(SEARCH, params={"scope": "all", "q": "tale"}).json()
        mark_family(api, "source-a", False)
        mark_family(api, "source-b")
        nothing = kid.get(SEARCH, params={"scope": "all", "q": "tale"}).json()
        assert nothing["next_cursor"] is None
        assert filtered["items"] == []
        assert filtered["next_cursor"] is None, "a page of dropped results left a cursor"
