"""M4 L26 (EF-INT-008, ENF-SEC-010, ENF-SEC-002, D14): importing a Mihon backup (`.tachibk`), through
the real application with real sessions, CSRF and keys.

A backup is read as a hostile file by a bounded subprocess. The import is two steps: a preview that
writes nothing to the library, and an application the admin asks for. A series of the backup is a
`pending_source_item` (its source is resolved later, through the Fetcher and its guard: no address
of the file is ever asked); the chapters it lists are known units; what the file proves read is a
`read_marker` of the profile the admin chose, and of nobody else; a tracker is a hint with no secret.

The attacks of the card: a forged file (a gzip bomb, a nesting, a huge string), the batch of someone
else, a child, a member, a key (read-only or not, an admin's included), a stale session.
"""

# ruff: noqa: F811  (the fixtures `api` and `world` are those of the child-surface suite)
from __future__ import annotations

import json
import socket
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
import structlog

from shelfhold.identity.public import local_auth
from tests import tachibk_env as env
from tests.auth_env import CSRF_HEADER
from tests.integration.test_child_surfaces import (  # noqa: F401  (fixtures)
    World,
    api,
    world,
)
from tests.visibility_env import add_profile

IMPORTS = "/api/v1/imports/tachibk"
UNKNOWN = "01HZZZZZZZZZZZZZZZZZZZZZZZ"
RAW = {"Content-Type": "application/octet-stream"}


@pytest.fixture(autouse=True)
def nothing_is_asked_of_the_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The addresses of a backup are never called by the import (resolution is deferred)."""
    asked: list[str] = []

    def tripwire(*_a: Any, **_k: Any) -> Any:
        asked.append("connect")
        raise AssertionError("the import reached for the network")

    monkeypatch.setattr(socket.socket, "connect", tripwire)
    return asked


def _upload(
    world: World, client: Any, data: bytes, profile: str | None = None, **headers: str
) -> Any:
    path = IMPORTS + (f"?profile_id={profile}" if profile else "")
    return client.post(
        path, content=data, headers={CSRF_HEADER: world.env.csrf(client), **RAW, **headers}
    )


def _apply(world: World, client: Any, import_id: str) -> Any:
    return client.post(f"{IMPORTS}/{import_id}/apply", headers={CSRF_HEADER: world.env.csrf(client)})


def _rows(world: World, sql: str, **params: Any) -> list[Any]:
    with world.seed.engine.connect() as connection:
        return list(connection.execute(sa.text(sql), params))


def _user(world: World, name: str) -> str:
    return str(_rows(world, "SELECT id FROM user WHERE username = :n", n=name)[0][0])


def _key_headers(world: World, username: str, **kwargs: Any) -> dict[str, str]:
    key = local_auth(world.seed.engine, world.env.settings).create_api_key(
        name=f"{username}-key", username=username, **kwargs
    )
    return {"Authorization": f"Bearer {key.value}"}


def _library_untouched(world: World) -> bool:
    return all(
        _rows(world, f"SELECT 1 FROM {table}") == []
        for table in ("pending_source_item", "read_marker", "tracker_hint")
    )


# -- the import itself -----------------------------------------------------------------------------


@pytest.mark.req("EF-INT-008")
def test_tachibk_import_pending_source_item_and_read_marker(world: World) -> None:
    ann = add_profile(world.env, "ann", "member", "10.0.0.11")
    ann_id = _user(world, "ann")
    previewed = _upload(world, world.env.client, env.sample(), ann_id)
    assert previewed.status_code == 201, previewed.text
    done = _apply(world, world.env.client, previewed.json()["id"])
    assert done.status_code == 200, done.text
    # a series of the backup is a pending item: its source is not resolved here
    items = _rows(
        world, "SELECT source_key, series_ref, title, memo FROM pending_source_item ORDER BY 1"
    )
    assert [(i.source_key, i.series_ref, i.title) for i in items] == [
        ("mihon:4242424242", "/series/orchard-tales", "Orchard Tales"),
        ("mihon:5151", "/series/harbor", "Harbor Notes"),
    ]
    # the chapters it lists are known units, read or not, in the order of the file
    memo = items[0].memo if isinstance(items[0].memo, dict) else json.loads(items[0].memo)
    assert memo["origin"] == "tachibk"
    assert [(u["ref"], u["state"]) for u in memo["units"]] == [
        (f"/c/{n}", "known") for n in (1, 2, 3, 4, 5)
    ]
    # what the file proves read is a marker of the chosen profile: two read, one started
    markers = _rows(
        world, "SELECT user_id, chapter_ref, \"read\", last_page FROM read_marker ORDER BY 2"
    )
    assert [(m.user_id, m.chapter_ref, bool(m.read), m.last_page) for m in markers] == [
        (ann_id, "/c/1", True, None), (ann_id, "/c/2", True, None), (ann_id, "/c/3", False, 7),
    ]  # fmt: skip
    # the tracker is a hint: which entry of which tracker, and how far the reader was there
    hints = _rows(world, "SELECT user_id, tracker, remote_id, last_chapter FROM tracker_hint")
    assert [tuple(h) for h in hints] == [(ann_id, "anilist", "7001", 40.0)]
    # the reserved action and the service each leave their line
    actions = [r[0] for r in _rows(world, "SELECT action FROM audit")]
    assert "import.tachibk" in actions and "import.tachibk_applied" in actions
    assert ann.get("/api/v1/me").status_code == 200


@pytest.mark.req("EF-INT-008")
def test_a_chapter_is_never_read_beyond_what_the_file_proves(world: World) -> None:
    ann_id = _user(world, "admin")
    mangas = [
        env.manga(
            9, "/s", "Proof",
            chapters=[
                env.chapter("/c/1", 1.0, read=True), env.chapter("/c/2", 2.0),
                env.chapter("/c/3", 3.0, read=False, page=0), env.chapter("/c/40", 40.0),
            ],
            trackers=[env.tracking(2, 5, last=40.0)],
        )
    ]  # fmt: skip
    made = _upload(world, world.env.client, env.tachibk(env.backup(mangas)), ann_id)
    assert _apply(world, world.env.client, made.json()["id"]).status_code == 200
    # the tracker is at chapter 40, a later chapter is read: the ones before it are not "read"
    rows = _rows(world, "SELECT chapter_ref, \"read\" FROM read_marker ORDER BY 1")
    assert [(r.chapter_ref, bool(r.read)) for r in rows] == [("/c/1", True)]
    # nothing reached the reading journal: a marker waits for its series to be resolved
    assert _rows(world, "SELECT 1 FROM reading_position") == []


@pytest.mark.req("EF-INT-008", "ENF-SEC-010")
def test_tachibk_preview_before_apply(world: World) -> None:
    previewed = _upload(world, world.env.client, env.sample())
    assert previewed.status_code == 201, previewed.text
    body = previewed.json()
    assert body["state"] == "previewed" and body["profile_id"] == _user(world, "admin")
    assert (body["series"], body["chapters"], body["read"], body["started"], body["trackers"]) == (
        2, 5, 2, 1, 1,
    )  # fmt: skip
    assert [i["title"] for i in body["items"]] == ["Orchard Tales", "Harbor Notes"]
    assert env.SOURCE_NAME not in previewed.text and "SynthSource" not in previewed.text
    # a preview writes nothing the library keeps
    assert _library_untouched(world)
    shown = world.env.client.get(f"{IMPORTS}/{body['id']}")
    assert shown.status_code == 200 and shown.json()["state"] == "previewed"
    applied = _apply(world, world.env.client, body["id"])
    assert applied.status_code == 200 and applied.json()["state"] == "applied"
    assert applied.json()["pending_items"] == 2 and applied.json()["markers"] == 3
    assert len(_rows(world, "SELECT 1 FROM pending_source_item")) == 2
    # the same preview is not applied twice
    again = _apply(world, world.env.client, body["id"])
    assert again.status_code == 409 and again.json()["code"] == "import.tachibk_applied"
    assert len(_rows(world, "SELECT 1 FROM read_marker")) == 3


@pytest.mark.req("EF-INT-008")
def test_importing_the_same_backup_twice_makes_no_duplicate(world: World) -> None:
    for _ in range(2):
        made = _upload(world, world.env.client, env.sample())
        assert _apply(world, world.env.client, made.json()["id"]).status_code == 200
    assert len(_rows(world, "SELECT 1 FROM pending_source_item")) == 2
    assert len(_rows(world, "SELECT 1 FROM read_marker")) == 3
    assert len(_rows(world, "SELECT 1 FROM tracker_hint")) == 1


@pytest.mark.req("EF-INT-008", "ENF-SEC-002")
def test_tachibk_bomb_and_depth_caps_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = {
        "decompressed_too_large": env.bomb(70),
        "too_deep": env.tachibk(
            env.backup([env.manga(1, "/u", "T", extra=env.nested_groups(40))])
        ),
        "too_many_entries": env.tachibk(
            env.backup([env.manga(1, "/u", "T", extra=b"".join(env.vi(90, 1) for _ in range(100_001)))])
        ),
        "string_too_long": env.tachibk(env.backup([env.manga(1, "/u", "x" * 5_000)])),
        "malformed": b"\x1f\x8b" + b"not really a gzip",
    }
    for reason, data in cases.items():
        answer = _upload(world, world.env.client, data)
        assert answer.status_code == 422, (reason, answer.text)
        assert answer.json()["code"] == "import.tachibk_invalid", reason
        assert answer.json()["reason"] == reason, reason
        assert "SynthSource" not in answer.text  # never an echo of the file
    # a body over the compressed bound is refused as it arrives: the decoder is never started
    started: list[bool] = []
    monkeypatch.setattr(
        "shelfhold.acquisition.application.tachibk_import.decode_in_sandbox",
        lambda *_a, **_k: started.append(True),
    )
    huge = _upload(world, world.env.client, b"\x1f\x8b" + bytes(16 * 1024 * 1024 + 1))
    assert huge.status_code == 422 and huge.json()["reason"] == "too_large"
    assert started == []
    assert _rows(world, "SELECT 1 FROM tachibk_import") == [] and _library_untouched(world)


@pytest.mark.req("EF-INT-008")
def test_tachibk_unknown_schema_version_refused_with_code(world: World) -> None:
    future = env.tachibk(env.backup([env.manga(1, "/u", "T")], extra=env.ld(999, "later")))
    answer = _upload(world, world.env.client, future)
    assert answer.status_code == 422, answer.text
    assert answer.json()["code"] == "import.tachibk_schema"
    assert answer.json()["reason"] == "unknown_schema"
    assert _rows(world, "SELECT 1 FROM tachibk_import") == [] and _library_untouched(world)


@pytest.mark.req("EF-INT-008", "ENF-SEC-015")
def test_tachibk_source_names_never_logged_nor_exported(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    from shelfhold.platform.export_all import EXCLUDED_TABLES, EXPORTED_TABLES

    caplog.set_level("DEBUG")
    with structlog.testing.capture_logs() as logs:
        made = _upload(world, world.env.client, env.sample())
        assert _apply(world, world.env.client, made.json()["id"]).status_code == 200
        world.env.client.get(f"{IMPORTS}/{made.json()['id']}")
        _upload(world, world.env.client, env.bomb(70))  # a refusal is logged too
    spoken = repr(logs) + caplog.text
    assert "SynthSource" not in spoken and env.SOURCE_NAME not in spoken
    assert env.SECRET not in spoken
    # the tables of the import stay out of the open export; the names are not stored at all
    mine = {"tachibk_import", "read_marker", "tracker_hint"}
    assert mine <= set(EXCLUDED_TABLES) and not mine & set(EXPORTED_TABLES)
    with world.seed.engine.connect() as connection:
        dump = "\n".join(connection.connection.iterdump())  # type: ignore[attr-defined]
    assert "SynthSource" not in dump and env.SECRET not in dump and "tracker.example" not in dump
    audit = " ".join(str(r) for r in _rows(world, "SELECT * FROM audit"))
    assert "SynthSource" not in audit and "Orchard" not in audit  # a title is not an audit line


@pytest.mark.req("EF-INT-008", "ENF-SEC-010")
def test_tachibk_read_markers_go_to_the_chosen_profile_only(world: World) -> None:
    add_profile(world.env, "ann", "member", "10.0.0.11")
    add_profile(world.env, "bob", "member", "10.0.0.12")
    ann_id, bob_id, admin_id = _user(world, "ann"), _user(world, "bob"), _user(world, "admin")
    made = _upload(world, world.env.client, env.sample(), ann_id)
    assert made.json()["profile_id"] == ann_id
    assert _apply(world, world.env.client, made.json()["id"]).status_code == 200
    owners = {r[0] for r in _rows(world, "SELECT user_id FROM read_marker")}
    owners |= {r[0] for r in _rows(world, "SELECT user_id FROM tracker_hint")}
    assert owners == {ann_id}  # not the admin who imported, not Bob, not the child
    assert bob_id not in owners and admin_id not in owners and world.kid_id not in owners
    # an absent profile is refused, and nothing is made for it
    ghost = _upload(world, world.env.client, env.sample(), UNKNOWN)
    assert ghost.status_code == 404 and ghost.json()["code"] == "import.tachibk_not_found"
    # a profile that is deleted between the preview and the application takes its markers with it
    other = _upload(world, world.env.client, env.sample(), bob_id)
    with world.seed.engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM user WHERE id = :u"), {"u": bob_id})
    gone = _apply(world, world.env.client, other.json()["id"])
    assert gone.status_code == 404
    assert bob_id not in {r[0] for r in _rows(world, "SELECT user_id FROM read_marker")}


@pytest.mark.req("EF-INT-008", "ENF-SEC-002")
def test_tachibk_stores_no_secret_and_no_path_and_calls_no_address(
    world: World, tmp_path: Any, nothing_is_asked_of_the_network: list[str]
) -> None:
    canary = tmp_path / "canary.txt"
    canary.write_text("untouched")
    hostile = env.manga(
        1, str(canary), "Hostile",
        [env.chapter("http://127.0.0.1:9/never", 1.0, read=True)],
        [env.tracking(2, 3, url=f"https://tracker.example/?key={env.SECRET}")],
    )  # fmt: skip
    data = env.tachibk(env.backup([hostile], extra=env.ld(104, f"token={env.SECRET}")))
    made = _upload(world, world.env.client, data)
    assert _apply(world, world.env.client, made.json()["id"]).status_code == 200
    # the path and the address are only text: nothing was opened, nothing was called
    assert canary.read_text() == "untouched" and nothing_is_asked_of_the_network == []
    assert _rows(world, "SELECT series_ref FROM pending_source_item") == [(str(canary),)]
    with world.seed.engine.connect() as connection:
        dump = "\n".join(connection.connection.iterdump())  # type: ignore[attr-defined]
    assert env.SECRET not in dump and "tracker.example" not in dump


# -- the attacks: who may import -------------------------------------------------------------------


@pytest.mark.req("EF-INT-008", "ENF-SEC-010", "EF-USR-024")
def test_tachibk_import_is_a_reserved_admin_action(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ann = add_profile(world.env, "ann", "member", "10.0.0.11")
    real = _upload(world, world.env.client, env.sample())
    assert real.status_code == 201, real.text
    batch = real.json()["id"]
    ran: list[bool] = []
    monkeypatch.setattr(
        "shelfhold.acquisition.application.tachibk_import.decode_in_sandbox",
        lambda *_a, **_k: ran.append(True),
    )
    # a member and a child are refused, on every door, before a byte is decoded
    for client in (ann, world.kid):
        for answer in (
            _upload(world, client, env.sample()),
            _upload(world, client, env.sample(), _user(world, "ann")),
            _apply(world, client, batch),
            client.get(f"{IMPORTS}/{batch}"),
        ):
            assert answer.status_code == 403, answer.text
            assert answer.json()["code"] == "forbidden"
    # a key never acts, whatever its scope and whoever holds it
    other = world.env.other_client("10.0.0.9")
    for username, flags in (("admin", {"read_only": True}), ("admin", {})):
        headers = _key_headers(world, username, **flags)
        for answer in (
            other.post(IMPORTS, content=env.sample(), headers={**headers, **RAW}),
            other.post(f"{IMPORTS}/{batch}/apply", headers=headers),
            other.get(f"{IMPORTS}/{batch}", headers=headers),
        ):
            assert answer.status_code in {401, 403}, (username, flags)
            assert answer.json()["code"] in {"credential_not_allowed", "forbidden"}
    assert world.env.other_client("10.0.0.8").post(IMPORTS, content=b"x").status_code in {401, 403}
    # a session that is not fresh is asked to log in again (a restricted profile exists)
    world.env.clock.advance(timedelta(minutes=20))
    stale = _upload(world, world.env.client, env.sample())
    assert stale.status_code == 401 and stale.json()["code"] == "reauth_required"
    assert _apply(world, world.env.client, batch).status_code == 401
    assert len(_rows(world, "SELECT 1 FROM tachibk_import")) == 1
    assert _library_untouched(world) and ran == []


@pytest.mark.req("EF-INT-008", "ENF-SEC-010")
def test_a_batch_that_is_absent_is_the_same_for_everyone(world: World) -> None:
    absent = world.env.client.get(f"{IMPORTS}/{UNKNOWN}")
    assert absent.status_code == 404 and absent.json()["code"] == "import.tachibk_not_found"
    missing = _apply(world, world.env.client, UNKNOWN)
    assert missing.status_code == 404 and missing.json()["code"] == "import.tachibk_not_found"
    badly = world.env.client.get(f"{IMPORTS}/not-an-ulid")
    assert badly.status_code == 404 and badly.json()["type"] == absent.json()["type"]


@pytest.mark.req("EF-INT-008", "ENF-SEC-002")
def test_a_decoder_that_fails_leaves_nothing_and_says_so_without_the_file(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shelfhold.platform.import_tachibk.decode import TachibkRefusedError

    def crash(*_a: Any, **_k: Any) -> Any:
        raise TachibkRefusedError("worker_failed", "import.tachibk_unavailable")

    monkeypatch.setattr("shelfhold.acquisition.application.tachibk_import.decode_in_sandbox", crash)
    answer = _upload(world, world.env.client, env.sample())
    assert answer.status_code == 503 and answer.json()["code"] == "import.tachibk_unavailable"
    assert "SynthSource" not in answer.text
    assert _rows(world, "SELECT 1 FROM tachibk_import") == [] and _library_untouched(world)
