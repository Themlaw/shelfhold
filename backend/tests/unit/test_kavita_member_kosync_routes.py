"""M5 L09 (EF-LEC-009, EF-USR-012, ENF-SEC-005, ADR-0053 section 6): the kosync routes of Kavita on the
closed list of a member's key, and nothing else of `Koreader`.

Kavita reads the key of the account **in the path** of these routes (`/api/Koreader/{apiKey}/...`):
the one place where a member's key is in a URL. It is put there by the client from its own key, never
by the caller; the call is logged under the template (never the key); and no other `Koreader` route
(`users/auth`, `users/create`) is reachable.
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from shelfhold.export.targets.kavita import accounts, routes
from shelfhold.export.targets.kavita.errors import ProtocolError, RouteNotAllowed
from tests.fakes.kavita import BASE_URL, FakeKavita, build_fetcher
from tests.kavita_env import documented_routes, run

GET = ("GET", "/api/Koreader/{apiKey}/syncs/progress/{ebookHash}")
PUT = ("PUT", "/api/Koreader/{apiKey}/syncs/progress")
DOCUMENT = "0123456789abcdef0123456789abcdef"


def _client(fake: FakeKavita, key: str) -> accounts.MemberKavitaClient:
    return accounts.MemberKavitaClient(build_fetcher(fake), BASE_URL, accounts.MemberKey(key))


@pytest.mark.req("EF-LEC-009", "EF-USR-012", "ENF-SEC-005")
def test_member_token_kosync_routes_on_closed_list() -> None:
    listed = {(r.method, r.path): r for r in accounts.MEMBER_ROUTES}
    assert GET in listed and PUT in listed
    assert listed[GET].write is False and listed[PUT].write is True
    # the whole `Koreader` reach of a member is these two: the auth check and the creation of an
    # account are in Kavita's controller and never on the list
    assert {p for _m, p in listed if "Koreader" in p} == {GET[1], PUT[1]}
    # the doc says the same thing as the code
    assert {GET, PUT} <= documented_routes()
    # a destination (the dedicated key) can never describe a call to them
    for method, path in (GET, PUT):
        with pytest.raises(RouteNotAllowed):
            routes.resolve(method, path)
    assert not {GET, PUT} & {(r.method, r.path) for r in routes.ALLOWED_ROUTES}


@pytest.mark.req("EF-LEC-009", "EF-USR-012", "ENF-SEC-005")
def test_member_kosync_calls_put_the_key_in_the_path_from_the_client_and_nowhere_else() -> None:
    fake = FakeKavita()
    mine = fake.add_member_key("mum.kv")
    client = _client(fake, mine)
    sent = run(
        client.call(
            "PUT", PUT[1],
            body={"document": DOCUMENT, "progress": "/body/DocFragment[2]/body/p", "percentage": 0.4,
                  "device": "Libra 2", "device_id": "A1"},
        )
    )  # fmt: skip
    assert sent.status == 200
    got = run(client.call("GET", GET[1], path_params={"ebookHash": DOCUMENT}))
    assert got.data["progress"] == "/body/DocFragment[2]/body/p" and got.data["percentage"] == 0.4
    assert [r.method for r in fake.requests] == ["PUT", "GET"]
    for request in fake.requests:
        assert request.path.startswith(f"/api/Koreader/{mine}/syncs/progress")  # the key, once
        assert "x-api-key" not in request.headers  # not twice
        assert mine not in request.raw_url.split("?", 1)[-1] and mine.encode() not in request.body
    assert fake.violations == []


@pytest.mark.req("EF-LEC-009", "EF-USR-012", "ENF-SEC-005")
@pytest.mark.parametrize(
    ("method", "path", "params"),
    [
        ("GET", "/api/Koreader/{apiKey}/users/auth", {}),
        ("POST", "/api/Koreader/{apiKey}/users/create", {}),
        ("PUT", "/api/Koreader/{apiKey}/syncs/progress/{ebookHash}", {"ebookHash": DOCUMENT}),
        ("GET", "/api/Koreader/{apiKey}/syncs/progress", {}),
        ("POST", PUT[1], {}),  # the method matters
        ("DELETE", GET[1], {"ebookHash": DOCUMENT}),
        ("GET", "/api/Koreader/x/syncs/progress/" + DOCUMENT, {}),  # a key written by the caller
        ("PUT", "/api/koreader/{apiKey}/syncs/progress/../../Reader/progress", {}),
        ("GET", "/api/Koreader/{apiKey}/syncs/progress/{ebookHash}/", {"ebookHash": DOCUMENT}),
    ],
)
def test_member_kosync_off_list_call_is_refused_before_any_request(
    method: str, path: str, params: dict[str, str]
) -> None:
    fake = FakeKavita()
    key = fake.add_member_key("mum.kv")
    with pytest.raises(RouteNotAllowed) as refused:
        run(_client(fake, key).call(method, path, path_params=params))
    assert key not in str(refused.value)
    assert fake.requests == [] and fake.violations == []


@pytest.mark.req("EF-LEC-009", "EF-USR-012", "ENF-SEC-005")
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"ebookHash": ""},
        {"ebookHash": "../../Account"},
        {"ebookHash": DOCUMENT + "/x"},
        {"ebookHash": DOCUMENT.upper()},  # the digest is the lower-case md5 the device sends
        {"ebookHash": DOCUMENT[:-1]},
        {"ebookHash": DOCUMENT, "apiKey": "another-members-key"},  # the key is never the caller's
    ],
)
def test_member_kosync_path_values_are_checked_and_the_key_is_never_supplied(
    params: dict[str, str],
) -> None:
    fake = FakeKavita()
    mine = fake.add_member_key("mum.kv")
    other = fake.add_member_key("dad.kv")
    with pytest.raises(RouteNotAllowed):
        run(_client(fake, mine).call(*GET, path_params={**params}))
    assert fake.requests == []
    assert fake.requests == [] and other  # nobody else's key was ever tried


@pytest.mark.req("EF-LEC-009", "ENF-SEC-005")
def test_member_key_in_the_kosync_path_is_never_logged() -> None:
    fake = FakeKavita()
    mine = fake.add_member_key("mum.kv")
    client = _client(fake, mine)
    with capture_logs() as logs:
        run(client.call("PUT", PUT[1], body={"document": DOCUMENT, "progress": "x",
                                              "percentage": 0.1, "device": "d"}))  # fmt: skip
        with pytest.raises(ProtocolError, match="400"):  # a book Kavita does not know: a 400
            run(client.call("GET", GET[1], path_params={"ebookHash": "f" * 32}))
    assert logs, "the calls are logged"
    assert mine not in repr(logs)
    fake.key_accounts.pop(mine)
    with capture_logs() as refused, pytest.raises(ProtocolError, match="Koreader"):
        run(client.call("PUT", PUT[1], body={"document": DOCUMENT}))
    assert mine not in repr(refused)
