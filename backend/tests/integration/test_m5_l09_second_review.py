"""M5 L09, second adverse review: two holes the first pass had left, one red proof each.

Both are found by reading the branch against its own claims, not by repeating what the lot and
its review already prove (the proposal after a device write, the spine outside the write lock,
the token as the device). Red on the head (`xfail` strict), each fails for the reason its
message says and turns loud (XPASS) the day it is closed:

* a percentage no float can hold (a JSON integer far past the range of one) is not the
  documented 422 of a wrong field but an unhandled overflow, and the device's door answers a 500
  (the same class of hole as the extreme date of L07, fixed there as a typed 422);
* the pass to Kavita sends the server's own name of a writer (`kosync:…`, a digest of the
  device token) as `device_id`, although the lot's own rule is that this name is never shown to
  a device — Kavita's own kosync answer then hands it to every reader that syncs against the
  member's account, and the device's own word sits right there in `client_id`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.reading.public import Kosync
from tests.integration.test_l46_device_tokens import kosync
from tests.integration.test_l47_kosync import PUT, positions, volume
from tests.integration.test_l48_opds import World, make_world
from tests.integration.test_m5_l09_kavita_epub import DIGEST, XPOINTER, epub_chapter
from tests.sync_env import SyncEnv, sync_env

XAUTH_USER = "kobo-token"  # the public identifier a device sends (`x-auth-user`), no secret
CLIENT_ID = "A1B2C3"  # what the Kobo says of itself (its own word, kept in `client_id`)


@pytest.fixture
def lab(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with make_world(monkeypatch, settings) as made:
        yield made


def _service(env: SyncEnv, monkeypatch: pytest.MonkeyPatch) -> Kosync:
    """The real kosync service on the engine and the clock of the sync harness. Only the format
    lookup is a seam (an `export_record` needs a whole destination): the file is an EPUB, which
    is what `_kind` would say from its record."""
    monkeypatch.setattr(Kosync, "_kind", lambda _self, _c, _item: "reflowable")
    return Kosync(env.engine, lambda: None, env.clock.now)  # type: ignore[arg-type, return-value]


@pytest.mark.req("EF-LEC-009", "EF-LEC-014")
def test_a_percentage_no_float_can_hold_is_a_422_not_a_500(lab: World) -> None:
    """The route's own contract: « What is wrong with a field is a 422 that names no value, for
    a body KOReader never sends (a field of the wrong type, a page that is not a number) ». A
    percentage outside [0, 1], NaN and the infinities are already a 422; an integer so large that
    no float can hold it (`json.loads` keeps it whole) is the same wrong field, but `float()`
    overflows before the bound is reached and the answer is the 500 of an unhandled error.
    Nothing is stored either way: the body is read before anything is written."""
    _item, digest = volume(lab, count=1, fmt="epub")
    body = {
        "document": digest, "progress": "3", "percentage": 10**400, "device": "Libra 2",
        "device_id": "A1B2C3",
    }  # fmt: skip
    client = TestClient(  # to read the answer of the unhandled exception, not to raise it
        lab.env.app,  # type: ignore[arg-type]
        base_url="https://testserver",
        client=("10.0.0.31", 40000),
        raise_server_exceptions=False,
    )
    answer = client.put(PUT, json=body, headers=kosync(lab.mum.public_id, lab.mum.secret))
    assert answer.status_code == 422, (answer.status_code, answer.text)
    assert positions(lab) == []


@pytest.mark.req("EF-LEC-009", "EF-USR-012")
def test_the_pass_to_kavita_never_sends_the_servers_name_of_a_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lot's own rule (its answer to m3): the server's name of a writer is never shown —
    `Kosync._in_frame` strips a `kosync:` identifier before answering a device, and the device's
    own word is what the locator keeps (`client_id`). The pass breaks the rule: `_pending_epub`
    reads the `device_id` column, which holds the server's name, and `save_epub` puts it in the
    body of Kavita's kosync route — whose answer every reader of that account then reads. What
    leaves is the device's own word, or nothing; never the name of a token of this server."""
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        _chapter = epub_chapter(env)
        assert _service(env, monkeypatch).push(
            user_id=reader, token=XAUTH_USER, document=DIGEST, progress=XPOINTER,
            percentage=0.5, device="Libra 2", device_id=CLIENT_ID,
        ) is True  # fmt: skip
        (row,) = env.positions(reader)
        # what the write really keeps: the server's name of the writer in the column, the
        # device's own word in the locator, and the walk (`who`) keyed by the same name
        assert row.device_id is not None and row.device_id.startswith("kosync:")
        assert row.locator["client_id"] == CLIENT_ID
        assert row.locator["who"] == row.device_id
        assert env.sync(reader).pushed == 1
        (write,) = env.fake.kosync_writes
        assert write["account"] == env.names[reader] and write["document"] == DIGEST
        said = write["body"].get("device_id")
        assert said is None or said == CLIENT_ID, (
            f"the server's name of the writer left for Kavita: {said!r}"
        )
