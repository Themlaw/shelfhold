"""M3 L40: the first-run wizard, part 2 (EF-UX-001 screens 4 and 5, EF-UX-007, EF-USR-023,
EF-INT-015, EF-SRC-024).

The routes are the real ones of `create_app()`. L40 opens no second door where one exists: the
shelves are L33's `precreate`, the kit is L07's page, the second admin is L05's `POST /profiles`,
the jurisdiction is `PATCH /config`, the first book is `POST /acquire`. What is new is the export
of the keyring (the only way out of the wizard that carries a secret) and the marking of the
sources "family" (D13), both reserved actions of the admin on a fresh session.

Every "never" of the lot is a test that tries to break it: the keyring leaves only as an age file
under a passphrase, and neither the passphrase nor a key is ever in an answer, a log, a replay
cache, the database or the diagnostic bundle; the kit holds no secret; the second admin and the
family marks are refused to a member, a child, a key and a stale session.
"""

# ruff: noqa: F811  (the `world` fixture of L39 is imported, then asked for by name)
from __future__ import annotations

import base64
import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.logging import configure_logging
from shelfhold.infrastructure.secrets.export import (
    MIN_PASSPHRASE_CHARS,
    ExportError,
    decrypt_export,
)
from tests.auth_env import db_dump, run_cli
from tests.diagnostic_env import Traps, leaks
from tests.integration.test_auth_keys_reserved import _bearer, _create_key
from tests.integration.test_l39_wizard import World, _call, _csrf, world  # noqa: F401
from tests.visibility_env import PASSWORD, add_profile

SCREENS = Path(__file__).resolve().parents[3] / "frontend/src/routes/assistant"
CATALOG = Path(__file__).resolve().parents[3] / "frontend/src/lib/i18n/fr.json"
STATE = "/api/v1/setup/state"
EXPORT = "/api/v1/setup/keyring-export"
FAMILY = "/api/v1/setup/sources/family"
KIT = "/api/v1/admin/recovery-kit"
PHRASE = "lampadaire bleu vingt-trois"
SECOND_ADMIN_PASSWORD = "un mot de passe long et nouveau"  # noqa: S105
SHELF_NAMES = ["Manga", "Comics et BD", "Livres", "Famille"]

NEW_ROUTES = (
    ("POST", EXPORT, {"passphrase": PHRASE}),
    ("PUT", FAMILY, {"source_keys": ["gutenberg"]}),
)
ADMIN_BODY = {"username": "second", "password": SECOND_ADMIN_PASSWORD, "role": "admin"}


def _state(world: World) -> dict[str, Any]:
    answer = world.api.client.get(STATE)
    assert answer.status_code == 200, answer.text
    return answer.json()  # type: ignore[no-any-return]


def _key_marks(world: World) -> list[str]:
    """Every form in which a key of the keyring could show: the clear JSON, each key in base64."""
    keyring = world.inst.comp.keyring()
    marks = [base64.b64encode(key.material).decode("ascii") for key in keyring.keys]
    return [*marks, keyring.to_json().decode("utf-8")]


def _exported(world: World) -> bool:
    return world.inst.comp.keyring().exported_at is not None


def _audit(world: World) -> list[str]:
    with world.inst.engine.connect() as connection:
        return list(connection.execute(sa.text("SELECT action FROM audit")).scalars())


def _profiles(world: World) -> list[str]:
    with world.inst.engine.connect() as connection:
        return sorted(connection.execute(sa.text("SELECT username FROM user")).scalars())


# -- the screens: at most five, texts in the catalogue --------------------------------------------


@pytest.mark.req("EF-UX-001")
def test_wizard_has_five_screens_in_all_and_no_more() -> None:
    """Parts 1 and 2 together: the five screens of the declared order, none else under
    `/assistant` (the sources, the jurisdiction and the kit are sections of them)."""
    screens = sorted(
        p.name for p in SCREENS.iterdir() if p.is_dir() and (p / "+page.svelte").exists()
    )
    assert screens == ["compte", "dossier", "etageres", "fin", "kavita"]


@pytest.mark.req("EF-UX-001")
def test_wizard_screens_say_nothing_that_is_not_in_the_catalogue() -> None:
    """No text node typed in a screen of part 2: every sentence goes through `t()`, and every key
    it names is in the catalogue."""
    catalog = json.loads(CATALOG.read_text("utf-8"))
    for screen in ("etageres", "fin"):
        source = (SCREENS / screen / "+page.svelte").read_text("utf-8")
        markup = re.sub(r"\{[^{}]*(\{[^{}]*\}[^{}]*)*\}", "", source.split("</script>", 1)[1])
        typed = [n.strip() for n in re.findall(r">([^<>]+)<", markup) if re.search(r"[^\W\d_]", n)]
        assert typed == [], (screen, typed)
        keys = re.findall(r"t\(\s*['\"]([a-z_.]+)['\"]", source)
        assert keys and all(key in catalog for key in keys), (screen, set(keys) - set(catalog))


# -- the state the screens read ------------------------------------------------------------------


@pytest.mark.req("EF-UX-001", "EF-UX-007")
def test_state_carries_the_sentences_of_the_shelves_the_server_will_make(world: World) -> None:
    """The screen shows the server's own sentences, before and after the shelves exist."""
    before = _state(world)["shelves"]
    assert [s["name"] for s in before] == SHELF_NAMES
    assert all(not s["created"] and s["phrase"] for s in before)
    assert before[0]["phrase"] == "Les mangas vont dans Kavita \u203a Manga"
    made = world.api.post("/api/v1/shelves/precreate", json={"adopted": []})
    assert made.status_code == 201, made.text
    assert [s["phrase"] for s in _state(world)["shelves"]] == [s["phrase"] for s in before]
    assert all(s["created"] for s in _state(world)["shelves"])


@pytest.mark.req("EF-UX-001", "EF-SRC-024")
def test_state_says_what_the_screens_may_not_guess(world: World) -> None:
    state = _state(world)
    assert state["keyring_exported"] is False
    assert state["jurisdiction"]["confirmed"] is False
    assert len(state["jurisdiction"]["country"]) == 2
    assert {s["key"]: s["family"] for s in state["sources"]} == {
        "gutenberg": False, "wikisource": False, "standardebooks": False,
    }  # fmt: skip


# -- the jurisdiction is confirmed by the admin, with the route that exists -----------------------


@pytest.mark.req("EF-UX-001", "EF-SRC-024")
def test_jurisdiction_is_confirmed_through_the_config_route_and_state_follows(
    world: World,
) -> None:
    assert _state(world)["jurisdiction"]["confirmed"] is False
    done = world.api.client.patch(
        "/api/v1/config",
        json={"values": {"instance.jurisdiction": "BE"}},
        headers=_csrf(world, world.api.client),
    )
    assert done.status_code == 200, done.text
    assert _state(world)["jurisdiction"] == {"country": "BE", "confirmed": True}


@pytest.mark.req("EF-UX-001", "EF-SRC-024")
def test_jurisdiction_that_is_no_country_is_refused_and_stays_unconfirmed(
    world: World,
) -> None:
    for value in ("", "FRA", "1!", "<script>"):
        answer = world.api.client.patch(
            "/api/v1/config",
            json={"values": {"instance.jurisdiction": value}},
            headers=_csrf(world, world.api.client),
        )
        assert answer.status_code in {400, 409, 422}, (value, answer.text)
    assert _state(world)["jurisdiction"]["confirmed"] is False


@pytest.mark.req("EF-UX-001", "EF-USR-024")
def test_jurisdiction_is_not_a_member_or_a_child_to_confirm(world: World) -> None:
    for role, ip in (("member", "10.0.0.5"), ("child", "10.0.0.6")):
        client = add_profile(
            world.api, role, role, ip, **({"max_age_rating": 10} if role == "child" else {})
        )
        answer = client.patch(
            "/api/v1/config",
            json={"values": {"instance.jurisdiction": "BE"}},
            headers=_csrf(world, client),
        )
        assert answer.status_code == 403, (role, answer.text)
    assert _state(world)["jurisdiction"]["confirmed"] is False


# -- the keyring export: an age file under a passphrase, never anything else ---------------------


@pytest.mark.req("EF-UX-001", "EF-INT-015", "ENF-SEC-005")
def test_keyring_export_is_an_age_file_the_phrase_opens_and_it_is_recorded(
    world: World,
) -> None:
    assert _state(world)["keyring_exported"] is False and not _exported(world)
    answer = world.api.post(EXPORT, json={"passphrase": PHRASE})
    assert answer.status_code == 200, answer.text
    assert answer.headers["content-disposition"].startswith("attachment")
    assert answer.headers["cache-control"] == "no-store"
    assert answer.headers["content-type"].startswith("text/plain")
    opened = decrypt_export(answer.text, PHRASE)
    assert [k.kid for k in opened.keys] == [k.kid for k in world.inst.comp.keyring().keys]
    with pytest.raises(ExportError):
        decrypt_export(answer.text, PHRASE + " x")
    assert _state(world)["keyring_exported"] is True and _exported(world)


@pytest.mark.req("EF-INT-015", "ENF-SEC-005")
def test_keyring_export_is_never_the_key_in_clear(world: World) -> None:
    answer = world.api.post(EXPORT, json={"passphrase": PHRASE})
    assert answer.status_code == 200
    seen = answer.text + str(answer.headers) + _state_text(world)
    for mark in _key_marks(world):
        assert mark not in seen
    assert answer.text.startswith("-----BEGIN AGE ENCRYPTED FILE-----")


def _state_text(world: World) -> str:
    return world.api.client.get(STATE).text


@pytest.mark.req("EF-UX-001", "ENF-SEC-005")
def test_keyring_export_refuses_a_short_phrase_and_never_echoes_it(world: World) -> None:
    short = "a" * (MIN_PASSPHRASE_CHARS - 1)
    for body in ({"passphrase": short}, {"passphrase": ""}, {}, {"passphrase": short, "x": 1}):
        answer = world.api.post(EXPORT, json=body)
        assert answer.status_code in {400, 422}, (body, answer.text)
        assert short not in answer.text and "keyring" not in answer.text.lower().replace(
            "keyring-export", ""
        )
    assert not _exported(world) and _state(world)["keyring_exported"] is False


@pytest.mark.req("EF-UX-001", "ENF-SEC-005")
def test_keyring_export_passphrase_in_the_query_string_is_not_read(world: World) -> None:
    """A phrase in a URL would be in every proxy log: only the body is read."""
    answer = world.api.client.post(
        f"{EXPORT}?passphrase={PHRASE.replace(' ', '+')}", headers=_csrf(world, world.api.client)
    )
    assert answer.status_code in {400, 422}
    assert not _exported(world)


@pytest.mark.req("EF-UX-001", "ENF-SEC-005")
def test_keyring_export_is_never_logged(
    world: World,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="DEBUG", log_format="json")  # type: ignore[arg-type]
    capsys.readouterr()
    answer = world.api.post(EXPORT, json={"passphrase": PHRASE})
    refused = world.api.post(EXPORT, json={"passphrase": "court"})
    assert answer.status_code == 200 and refused.status_code in {400, 422}
    out = capsys.readouterr()
    text = out.out + out.err
    assert text  # debug level: something was said
    for secret in (PHRASE, answer.text, *_key_marks(world)):
        assert secret not in text
    assert "BEGIN AGE ENCRYPTED FILE" not in text


@pytest.mark.req("EF-UX-001", "ENF-SEC-005")
def test_keyring_export_is_never_kept_for_a_replay_nor_in_the_database(
    world: World,
) -> None:
    headers = {"Idempotency-Key": "wizard-export-1"}
    first = world.api.post(EXPORT, json={"passphrase": PHRASE}, headers=headers)
    again = world.api.post(EXPORT, json={"passphrase": PHRASE}, headers=headers)
    assert first.status_code == again.status_code == 200
    assert "Idempotent-Replayed" not in again.headers
    dump = db_dump(world.api.settings)
    assert PHRASE not in dump and first.text not in dump and "BEGIN AGE ENCRYPTED" not in dump
    for mark in _key_marks(world):
        assert mark not in dump
    world.api.clock.advance(timedelta(minutes=20))
    stale = world.api.post(EXPORT, json={"passphrase": PHRASE}, headers=headers)
    assert stale.status_code == 401 and stale.json()["code"] == "reauth_required"
    assert "BEGIN AGE" not in stale.text


@pytest.mark.req("EF-UX-001", "ENF-SEC-005", "ENF-LEG-009")
def test_keyring_export_is_not_in_the_diagnostic_bundle(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    answer = world.api.post(EXPORT, json={"passphrase": PHRASE})
    assert answer.status_code == 200
    out = tmp_path / "out" / "bundle.zip"
    out.parent.mkdir()
    assert run_cli(monkeypatch, world.api.settings, "diagnostic", "bundle", "--out", str(out)) == 0
    traps = Traps(values={"phrase": PHRASE, "armored": answer.text.splitlines()[1]})
    for index, mark in enumerate(_key_marks(world)):
        traps.values[f"key_{index}"] = mark
    assert leaks(out.read_bytes(), traps) == []


@pytest.mark.req("EF-UX-001", "EF-USR-024")
@pytest.mark.parametrize(("method", "path", "body"), NEW_ROUTES)
def test_new_wizard_routes_are_refused_to_a_member_and_to_a_child(
    world: World,
    method: str,
    path: str,
    body: Any,
) -> None:
    before = (_exported(world), _state(world)["sources"])
    for role, ip in (("member", "10.0.0.5"), ("child", "10.0.0.6")):
        client = add_profile(
            world.api, role, role, ip, **({"max_age_rating": 10} if role == "child" else {})
        )
        answer = _call(client, method, path, body, _csrf(world, client))
        assert answer.status_code == 403, (role, answer.text)
        assert answer.json()["code"] == "forbidden"
        assert "BEGIN AGE" not in answer.text
    assert (_exported(world), _state(world)["sources"]) == before


@pytest.mark.req("EF-UX-001", "EF-USR-024")
@pytest.mark.parametrize(("method", "path", "body"), NEW_ROUTES)
def test_new_wizard_routes_are_refused_to_an_api_key_even_an_admin_one(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    method: str,
    path: str,
    body: Any,
) -> None:
    key = _create_key(monkeypatch, capsys, world.api.settings)
    answer = _call(world.api.other_client("10.0.0.9"), method, path, body, _bearer(key))
    assert answer.status_code in {401, 403}, answer.text
    assert answer.json()["code"] in {"credential_not_allowed", "forbidden"}
    assert not _exported(world) and "BEGIN AGE" not in answer.text


@pytest.mark.req("EF-UX-001", "EF-USR-024")
@pytest.mark.parametrize(("method", "path", "body"), NEW_ROUTES)
def test_new_wizard_routes_need_a_session_and_a_fresh_one(
    world: World,
    method: str,
    path: str,
    body: Any,
) -> None:
    assert _call(world.api.other_client("10.0.0.8"), method, path, body).status_code == 401
    world.api.clock.advance(timedelta(minutes=20))
    stale = _call(world.api.client, method, path, body, _csrf(world, world.api.client))
    assert stale.status_code == 401 and stale.json()["code"] == "reauth_required"
    assert not _exported(world)


@pytest.mark.req("EF-UX-001", "EF-USR-024", "ENF-OBS-003")
def test_the_export_and_the_family_marks_leave_a_line_in_the_audit(world: World) -> None:
    assert world.api.post(EXPORT, json={"passphrase": PHRASE}).status_code == 200
    assert (
        world.api.client.put(
            FAMILY, json={"source_keys": ["gutenberg"]}, headers=_csrf(world, world.api.client)
        ).status_code
        == 200
    )
    assert {"secrets.export", "source.mark_family"} <= set(_audit(world))


@pytest.mark.req("EF-UX-001", "EF-USR-024")
def test_a_refused_export_leaves_no_line_that_says_it_happened(world: World) -> None:
    client = add_profile(world.api, "member", "member", "10.0.0.5")
    assert (
        _call(client, "POST", EXPORT, {"passphrase": PHRASE}, _csrf(world, client)).status_code
        == 403
    )
    assert "secrets.export" not in _audit(world)


# -- the recovery kit: the L07 page, no secret in it ---------------------------------------------


@pytest.mark.req("EF-USR-023", "EF-UX-001")
def test_recovery_kit_of_the_wizard_is_the_printable_page_and_holds_no_secret(
    world: World,
) -> None:
    assert world.api.post(EXPORT, json={"passphrase": PHRASE}).status_code == 200
    kit = world.api.client.get(KIT)
    assert kit.status_code == 200 and "text/html" in kit.headers["content-type"]
    assert kit.headers["cache-control"] == "no-store"
    for secret in (PHRASE, SECOND_ADMIN_PASSWORD, PASSWORD, *_key_marks(world)):
        assert secret not in kit.text
    assert "BEGIN AGE" not in kit.text and "<script" not in kit.text.lower()


@pytest.mark.req("EF-USR-023", "EF-UX-001")
def test_the_screen_opens_the_kit_by_its_route_and_writes_no_html_of_its_own() -> None:
    """The page is served with its own CSP, by hash: the screen links to it, it never copies the
    markup of an answer into a window (`document.write`)."""
    source = (SCREENS / "fin" / "+page.svelte").read_text("utf-8")
    assert "/api/v1/admin/recovery-kit" in source
    assert "document.write" not in source and "{@html" not in source


# -- the second admin: L05's route, with the guards of a reserved action -------------------------


@pytest.mark.req("EF-UX-001", "EF-USR-001", "EF-USR-024")
def test_second_admin_is_created_by_the_admin_on_a_fresh_session_and_audited(
    world: World,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="DEBUG", log_format="json")  # type: ignore[arg-type]
    capsys.readouterr()
    made = world.api.post("/api/v1/profiles", json=ADMIN_BODY)
    assert made.status_code == 201, made.text
    assert made.json()["role"] == "admin" and "second" in _profiles(world)
    assert "profile.create" in _audit(world)
    out = capsys.readouterr()
    for blob in (made.text, out.out + out.err, db_dump(world.api.settings)):
        assert SECOND_ADMIN_PASSWORD not in blob


@pytest.mark.req("EF-UX-001", "EF-USR-001", "EF-USR-024")
def test_second_admin_is_refused_to_a_member_a_child_a_key_and_a_stale_session(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for role, ip in (("member", "10.0.0.5"), ("child", "10.0.0.6")):
        client = add_profile(
            world.api, role, role, ip, **({"max_age_rating": 10} if role == "child" else {})
        )
        answer = client.post("/api/v1/profiles", json=ADMIN_BODY, headers=_csrf(world, client))
        assert answer.status_code == 403, (role, answer.text)
    key = _create_key(monkeypatch, capsys, world.api.settings)
    keyed = world.api.other_client("10.0.0.9").post(
        "/api/v1/profiles", json=ADMIN_BODY, headers=_bearer(key)
    )
    assert keyed.status_code in {401, 403}
    world.api.clock.advance(timedelta(minutes=20))
    stale = world.api.client.post(
        "/api/v1/profiles", json=ADMIN_BODY, headers=_csrf(world, world.api.client)
    )
    assert stale.status_code == 401 and stale.json()["code"] == "reauth_required"
    assert "second" not in _profiles(world)


@pytest.mark.req("EF-UX-001", "EF-USR-001")
def test_there_is_no_third_admin(world: World) -> None:
    assert world.api.post("/api/v1/profiles", json=ADMIN_BODY).status_code == 201
    third = world.api.post("/api/v1/profiles", json={**ADMIN_BODY, "username": "third"})
    assert third.status_code == 409 and third.json()["reason"] == "too_many_admins"
    assert "third" not in _profiles(world)


# -- the sources marked "family": reserved, audited, and only the ones the wizard proposes -------


@pytest.mark.req("EF-UX-001", "EF-USR-014")
def test_family_mark_opens_the_sources_the_admin_chose_and_nothing_else(world: World) -> None:
    done = world.api.client.put(
        FAMILY,
        json={"source_keys": ["gutenberg", "wikisource"]},
        headers=_csrf(world, world.api.client),
    )
    assert done.status_code == 200, done.text
    assert {s["key"]: s["family"] for s in _state(world)["sources"]} == {
        "gutenberg": True, "wikisource": True, "standardebooks": False,
    }  # fmt: skip
    # the mark is the one the child's search and the policy rules read
    from shelfhold.identity.adapters.store import IdentityStore

    store = IdentityStore(world.inst.engine)
    assert store.source_family("gutenberg") and not store.source_family("standardebooks")


@pytest.mark.req("EF-UX-001", "EF-USR-014")
def test_family_mark_refuses_a_source_that_is_not_proposed_and_writes_nothing(
    world: World,
) -> None:
    for keys in (
        ["suwayomi"],
        ["gutenberg", "suwayomi"],
        ["opds"],
        ["nope"],
        [""],
        ["gutenberg", "gutenberg"],
    ):
        answer = world.api.client.put(
            FAMILY, json={"source_keys": keys}, headers=_csrf(world, world.api.client)
        )
        assert answer.status_code in {400, 409, 422}, (keys, answer.text)
    assert not any(s["family"] for s in _state(world)["sources"])


@pytest.mark.req("EF-UX-001", "EF-USR-014")
def test_family_mark_can_be_undone_by_the_same_door(world: World) -> None:
    for keys in (["gutenberg"], []):
        assert (
            world.api.client.put(
                FAMILY, json={"source_keys": keys}, headers=_csrf(world, world.api.client)
            ).status_code
            == 200
        )
    assert not any(s["family"] for s in _state(world)["sources"])
