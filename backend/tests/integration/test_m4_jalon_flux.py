"""M4 milestone review, axis "flux" (contradicteur de jalon M4): what the lots do to each other.

Each case is red today (`xfail(strict=True)`) and turns green with the fix it names.

J-FLUX-1. The export of a profile (EF-USR-013: "l'export JSON contient ses données") is the M3
list of sections. The personal tables that M4 added are not in it: the e-readers paired for her
(`device_token`, L46), her own collections and reading lists (`collection`, L18a / L31), her
Kavita accounts (`destination_account`, L12), the destination of her notices
(`notify_destination`, L39), what a backup proved she read (`read_marker`, `tracker_hint`, L26),
her imported request lists (`request_list_import`, L31). `SqlIdentityStore.export_rows` picks its
tables one by one, so a new one never joins by itself.
"""

from collections.abc import Iterator
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.identity.public import LocalAuth
from shelfhold.infrastructure.ids import new_ulid
from tests.auth_env import CSRF_HEADER, PASSWORD, AuthEnv, auth_env, db_rows
from tests.integration.test_user_delete_export import _sql

pytestmark = pytest.mark.req("EF-USR-013")

PROFILES = "/api/v1/profiles"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


def _auth(env: AuthEnv) -> LocalAuth:
    return env.app.state.auth_gate.auth  # type: ignore[attr-defined,no-any-return]


def _export(env: AuthEnv, user_id: str) -> Any:
    assert env.login().status_code == 200
    response = env.client.get(
        f"{PROFILES}/{user_id}/export", headers={CSRF_HEADER: env.csrf(env.client)}
    )
    assert response.status_code == 200, response.text
    return response


def _reading_list(env: AuthEnv, owner_id: str | None, name: str) -> str:
    collection_id = new_ulid()
    _sql(
        env,
        "INSERT INTO collection (id, kind, name, owner_id, source) "
        "VALUES (?, 'reading_list', ?, ?, 'manual')",
        collection_id, name, owner_id,
    )  # fmt: skip
    return collection_id


@pytest.mark.xfail(
    strict=True,
    reason="M4 jalon J-FLUX-1: the profile export omits the M4 personal tables "
    "(device tokens, own reading lists)",
)
def test_the_export_of_a_profile_holds_her_e_readers_and_her_own_lists(api: AuthEnv) -> None:
    """A member with an e-reader paired for her and a reading list of her own: both are her data
    and are in her export. Another member's token and list are not."""
    auth = _auth(api)
    mum = auth.profile_add("mum", PASSWORD, "member")
    dad = auth.profile_add("dad", PASSWORD, "member")
    hers = auth.create_device_token(subject_username="mum", name="Kobo of mum")
    other = auth.create_device_token(subject_username="dad", name="Kobo of dad")
    her_list = _reading_list(api, mum, "Summer reading")
    other_list = _reading_list(api, dad, "Other list")
    assert db_rows(api.settings, "SELECT COUNT(*) FROM device_token WHERE subject_user_id = ?", mum)
    text = _export(api, mum).text
    # never another member's
    assert other.public_id not in text and other_list not in text
    # hers: the e-reader (its public identifier, never its secret) and her own list
    assert hers.secret not in text
    missing = [what for what, marker in (("device_token", hers.public_id), ("collection", her_list))
               if marker not in text]  # fmt: skip
    assert not missing, f"the export of her profile leaves out: {missing}"
