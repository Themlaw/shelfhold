"""Counter-review PoC of #207 (M3 L19): what B1 still lets through.

Red on the branch as it stands: it states what must hold and shows that it does not.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.ids import new_ulid
from tests.auth_env import PASSWORD, db_rows, run_cli
from tests.federated_env import FakeSource, hit, instance
from tests.integration.test_family_search import mark_family, set_profile
from tests.visibility_env import HIDDEN_TITLES, add_profile, seed_catalogue

SEARCH = "/api/v1/search"
ENTRY = "urn:book:harbor-sixteen"


def bind_feed_book(database: str, key: str, remote_id: str, work_id: str, edition_id: str) -> None:
    """The catalogue holds a book of a feed source as acquisition stores it: `remote_id` is the
    feed page the admin added it from, `memo["entry"]` the book on that page."""
    connection = sqlite3.connect(database)
    try:
        item_id = new_ulid()
        connection.execute(
            "INSERT INTO source_item (id, plugin_id, source_key, remote_id, memo) "
            "VALUES (?, ?, ?, ?, ?)",
            (item_id, key, key, remote_id, json.dumps({"entry": ENTRY})),
        )
        connection.execute(
            "INSERT INTO source_binding (id, work_id, edition_id, source_item_id) "
            "VALUES (?, ?, ?, ?)", (new_ulid(), work_id, edition_id, item_id),
        )  # fmt: skip
        connection.commit()
    finally:
        connection.close()


@pytest.mark.req("EF-USR-014", "EF-USR-006")
def test_counter_review_poc_a_rated_feed_book_comes_back_by_another_query(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """A feed source (OPDS, and the `safe` source built on it) names a book by the search page it
    is on: `remote_id` is the feed path *with the query*, the book is `memo["entry"]`. The admin
    added the book from the query "harbor" and rated it 16+. The child searches another query: the
    same book comes back with another `remote_id`, `_classified` (source_key + remote_id) does not
    see the binding, and the title the admin refused her is offered as unrated."""
    above = HIDDEN_TITLES["above"]
    source = FakeSource(
        "source-a", name="Source A",
        pages=[[hit("/feeds/search?query=sixteen", above, memo={"entry": ENTRY})]],
    )  # fmt: skip
    with instance(monkeypatch, settings, source) as api:
        seed = seed_catalogue(api)
        bind_feed_book(
            str(api.settings.database_path), "source-a", "/feeds/search?query=harbor",
            seed.works["above"], seed.editions["above"],
        )  # fmt: skip
        mark_family(api, "source-a")
        add_profile(api, "kid10", "child", "10.0.0.44", max_age_rating=10)
        (kid_id,) = db_rows(api.settings, "SELECT id FROM user WHERE username = 'kid10'")[0]
        set_profile(api, str(kid_id), hide_unrated_remote=0)
        assert run_cli(monkeypatch, api.settings, "search", "rebuild") == 0
        client = api.other_client("10.0.0.45")
        assert api.login("kid10", PASSWORD, client=client).status_code == 200
        answer = client.get(SEARCH, params={"scope": "all", "q": "sixteen"})
        assert answer.status_code == 200, answer.text
        assert source.calls, "the family source was not asked: the PoC proves nothing"
        assert above not in answer.text, "a rated 16+ book came back by another query"
        seed.engine.dispose()
