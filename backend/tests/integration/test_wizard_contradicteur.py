"""M3 ENF-UX-001, contradicteur de la #258: the door of the first account.

The secret of the compose holds at least 24 characters (`openssl rand -hex 24` gives 48): it is
never guessed, ten tries a minute per address are already far more than enough. The counter of
the door as a whole (`SETUP_THROTTLE_KEY`, 30 a minute from any address) adds no protection to
that secret; what it adds is a way for anyone who reaches a new instance to keep its installer
out: three addresses, ten wrong guesses each, again every minute, and the right secret typed from
the installer's own address answers 429. Unlike the sign-in, the door has no device cookie that
would let its owner through.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import SecretStr

from shelfhold.config import Settings
from tests.auth_env import PASSWORD, AuthEnv, auth_env, db_rows

TOKEN = "synthetic-setup-token-0123456789abcdef"  # noqa: S105 - throwaway test value
ROUTE = "/api/v1/setup/admin"
BODY = {"username": "owner", "password": PASSWORD, "setup_token": TOKEN}
OTHERS = ("198.51.100.1", "198.51.100.2", "198.51.100.3")  # documentation ranges (RFC 5737)
INSTALLER = "203.0.113.9"


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    """A new instance: no account, the secret of the compose set."""
    with auth_env(
        monkeypatch,
        settings.model_copy(update={"setup_token": SecretStr(TOKEN)}),
        with_admin=False,
    ) as env:
        yield env


@pytest.mark.req("EF-UX-001", "ENF-SEC-008")
def test_review_wizard_poc_other_addresses_cannot_shut_the_door_on_the_installer(
    fresh: AuthEnv,
) -> None:
    for n, ip in enumerate(OTHERS):
        caller = fresh.other_client(ip)
        for k in range(10):  # within the limit of each address
            guess = {**BODY, "setup_token": f"guess-{n}-{k:040d}"}
            assert caller.post(ROUTE, json=guess).status_code == 403
    made = fresh.other_client(INSTALLER).post(ROUTE, json=BODY)
    assert made.status_code == 201, made.text  # today: 429, the door is shut for everyone
    rows = db_rows(fresh.settings, "SELECT username FROM user WHERE role='admin'")
    assert [str(r[0]) for r in rows] == ["owner"]
