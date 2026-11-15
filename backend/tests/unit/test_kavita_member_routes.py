"""M4 L12 (D8): a member's own Kavita key reaches a closed list of routes, and nothing else.

The key of one member is not the dedicated administrator key: it lets Shelfhold ask who the key is
(once, when the key is pasted) and read and write that member's reading progress (L14). It never
reaches a library, a scan, a cover, the list of users, the metadata of a series or a key route.
The list is written here on purpose, independently of the code under test.
"""

import pytest

from shelfhold.export.targets.kavita import accounts, routes
from shelfhold.export.targets.kavita.dto import library_filter
from shelfhold.export.targets.kavita.errors import RouteNotAllowed
from shelfhold.infrastructure.kavita_names import same_kavita_username
from tests.fakes.kavita import BASE_URL, FakeKavita, build_fetcher
from tests.kavita_env import documented_routes, run

THE_CLOSED_LIST = {
    ("GET", "/api/Account"),  # who the key is: checked once, when it is pasted
    ("GET", "/api/Reader/get-progress"),
    ("POST", "/api/Reader/progress"),
    ("GET", "/api/Reader/continue-point"),
    ("POST", "/api/Series/all-v2"),  # the series list with the account's own pages read
}
SHARED = {("POST", "/api/Series/all-v2")}  # a read both keys make, each with its own key


def _client(fake: FakeKavita, key: str) -> accounts.MemberKavitaClient:
    return accounts.MemberKavitaClient(build_fetcher(fake), BASE_URL, accounts.MemberKey(key))


@pytest.mark.req("EF-USR-012")
def test_member_token_only_used_for_progress_routes() -> None:
    assert {(r.method, r.path) for r in accounts.MEMBER_ROUTES} == THE_CLOSED_LIST
    # none is a route of the dedicated key, and the dedicated client refuses all of them
    admin = {(r.method, r.path) for r in (*routes.ALLOWED_ROUTES, *routes.SETUP_ROUTES)}
    assert not admin & (THE_CLOSED_LIST - SHARED)
    for method, path in THE_CLOSED_LIST - SHARED:
        with pytest.raises(RouteNotAllowed):
            routes.resolve(method, path)
    # no member route changes a library or a series: the only writes are progress
    assert {r.path for r in accounts.MEMBER_ROUTES if r.write} == {"/api/Reader/progress"}
    # the doc says the same thing as the code
    assert documented_routes() >= THE_CLOSED_LIST


@pytest.mark.req("EF-USR-012")
@pytest.mark.parametrize(
    ("method", "path"),
    [
        *[
            (r.method, r.path)
            for r in (*routes.ALLOWED_ROUTES, *routes.SETUP_ROUTES)
            if (r.method, r.path) not in SHARED
        ],
        ("GET", "/api/Users"),
        ("POST", "/api/Library/scan-folder"),
        ("DELETE", "/api/Account/auth-key"),
        ("GET", "/api/Account/auth-keys"),
        ("POST", "/api/Account/rotate-auth-key"),
        ("POST", "/api/Account/login"),
        ("GET", "/api/Plugin/version"),  # takes the key in the query
        ("POST", "/api/Plugin/authenticate"),  # idem
        ("GET", "/api/Reader/progress"),  # the method matters
        ("POST", "/api/Reader/get-progress"),
        ("DELETE", "/api/Reader/progress"),
        ("POST", "/api/Reader/progress/../../Library/scan-folder"),
        ("POST", "/api/Reader/progress?apiKey=x"),
        ("GET", "/api/Account/"),
        ("GET", "//api/Account"),
        ("GET", "/api/Reader/get-progress%2f..%2fUsers"),
        ("GET", "http://elsewhere.example/api/Account"),
        ("GET", "/api/Reader/get-progress\n/api/Users"),
    ],
)
def test_member_key_off_list_call_is_refused_before_any_request(method: str, path: str) -> None:
    fake = FakeKavita()
    key = fake.add_member_key("mum.kv")
    client = _client(fake, key)
    with pytest.raises(RouteNotAllowed) as refused:
        run(client.call(method, path))
    assert key not in str(refused.value)
    assert fake.requests == [] and fake.violations == []


@pytest.mark.req("EF-USR-012")
def test_member_client_sends_the_key_in_a_header_to_the_listed_routes_only() -> None:
    fake = FakeKavita()
    mine = fake.add_member_key("mum.kv")
    client = _client(fake, mine)
    assert run(client.whoami()).username == "mum.kv"
    run(client.call("GET", "/api/Reader/get-progress", query={"chapterId": 4}))
    run(
        client.call(
            "POST",
            "/api/Reader/progress",
            body={"volumeId": 1, "chapterId": 4, "pageNum": 3, "seriesId": 2, "libraryId": 7},
        )
    )
    run(client.call("GET", "/api/Reader/continue-point", query={"seriesId": 2}))
    run(
        client.call(
            "POST",
            "/api/Series/all-v2",
            query={"PageNumber": 1, "PageSize": 100},
            body=library_filter(7),
        )
    )
    assert {(r.method, r.path) for r in fake.requests} == THE_CLOSED_LIST
    for request in fake.requests:
        assert request.headers["x-api-key"] == mine
        assert mine not in request.raw_url and mine.encode() not in request.body
    assert fake.violations == []
    # the dedicated key never travels through the member client
    assert not [r for r in fake.requests if r.headers["x-api-key"] != mine]


@pytest.mark.req("EF-USR-012", "ENF-SEC-005")
def test_the_member_key_does_not_print_and_the_answer_is_only_a_name() -> None:
    fake = FakeKavita()
    mine = fake.add_member_key("mum.kv")
    key = accounts.MemberKey(mine)
    assert mine not in repr(key) and mine not in str(key) and mine not in f"{key!r}{key}"
    assert key.reveal() == mine
    client = _client(fake, mine)
    assert mine not in repr(client) and mine not in str(client)
    # `GET /api/Account` carries a session token and the account's own key: neither is returned
    answer = run(client.whoami())
    assert answer.username == "mum.kv"
    assert "synthetic-session-token" not in repr(answer)
    assert "synthetic-default-key" not in repr(answer)
    with pytest.raises(ValueError, match="empty"):
        accounts.MemberKey("")


@pytest.mark.req("EF-USR-012")
@pytest.mark.parametrize("name", ["mum.kv", "MUM.KV", "Mum.Kv"])
def test_kavita_compares_the_account_name_without_case(name: str) -> None:
    assert same_kavita_username("mum.kv", name)
    assert not same_kavita_username("mum.kv", "mum.kv2")
    assert not same_kavita_username("mum.kv", "")
