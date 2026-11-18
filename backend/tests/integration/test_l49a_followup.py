"""M4 L49a, suites de la relecture adverse de #291 : les créneaux, l'historique, le porteur d'alias.

Les preuves d'origine sont dans `test_l49a_contradicteur.py`. Ici, ce que la correction ajoute :
le bail d'un créneau (expiration, durée totale, descripteurs rendus), un vrai client qui coupe sa
connexion, l'historique des parties et des numéros qui survit à une restauration, le porteur
d'alias qui n'est plus un tome de plage, un chapitre qui ment sur son CRC.
"""

# ruff: noqa: F401, F811  (the `lab` fixture is imported, then taken as an argument)
from __future__ import annotations

import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import bundle, export
from shelfhold.reading.bundle import BundleBusyError, BundleGoneError, BundleLimits
from shelfhold.reading.bundle.gate import Gate
from tests.bundle_env import Chapter, page
from tests.integration.test_l48_opds import World
from tests.integration.test_l49a_bundle import (
    complete,
    drain,
    fresh_token,
    lab,
    library,
    provider,
    scope_of,
    user_id,
)

pytestmark = pytest.mark.req("EF-INT-007", "ENF-SEC-010")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Held:
    """A descriptor held with a slot."""

    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


# -- the lease of a slot -----------------------------------------------------------------------


def test_a_lease_with_no_progress_is_taken_back_and_its_descriptors_are_closed() -> None:
    clock = Clock()
    gate = Gate(per_token=1, per_instance=2, send_timeout_s=60, monotonic=clock)
    held = Held()
    lost = gate.acquire("t1")
    lost.hold([held])
    clock.now = 59
    with pytest.raises(BundleBusyError):
        gate.acquire("t1")  # still within the timeout: the token has its one block
    assert not lost.lost and held.closed == 0
    clock.now = 61
    again = gate.acquire("t1")  # no progress for 61 s: the slot is taken back
    assert lost.lost and held.closed == 1 and gate.active() == 1
    again.release()
    lost.release()  # giving back a lease already taken back is harmless
    assert gate.active() == 0


def test_a_lease_that_makes_progress_keeps_its_slot_until_the_maximum_duration() -> None:
    clock = Clock()
    gate = Gate(per_token=1, per_instance=1, send_timeout_s=60, max_duration_s=300, monotonic=clock)
    lease = gate.acquire("t1")
    for at in range(50, 300, 50):  # a chunk taken every 50 s
        clock.now = at
        lease.progress()
        with pytest.raises(BundleBusyError):
            gate.acquire("t2")
    clock.now = 301  # over the whole duration: whatever its rate
    gate.acquire("t2")
    assert lease.lost


def test_a_released_slot_is_given_back_once_even_if_released_twice() -> None:
    gate = Gate(per_token=2, per_instance=2)
    a, b = gate.acquire("t"), gate.acquire("t")
    a.release()
    a.release()
    assert gate.active() == 1
    b()
    assert gate.active() == 0


def test_the_stream_stops_when_its_slot_was_taken_back(lab: World) -> None:
    lib = library(lab)
    lib.add(Chapter("1", [("1.png", page("png", "slow", 600_000))]))
    clock = Clock()
    api = provider(lab, limits=BundleLimits(chunk_bytes=16_384, send_timeout_s=30), monotonic=clock)
    who, scope, edition = user_id(lab, "mum"), scope_of(lab, "mum"), lab.seed.editions["ok"]
    stalled = api.open(user_id=who, scope=scope, edition_id=edition, token_id=fresh_token(lab))
    next(iter(stalled.chunks))
    clock.now = 31  # the client takes nothing for longer than the send timeout
    other = api.open(  # another token: the instance has a second slot, the stalled one is gone
        user_id=who, scope=scope, edition_id=edition, token_id=fresh_token(lab)
    )
    third = api.open(user_id=who, scope=scope, edition_id=edition, token_id=fresh_token(lab))
    with pytest.raises(BundleGoneError):
        next(iter(stalled.chunks))
    for download in (other, third):
        download.chunks.close()  # type: ignore[attr-defined]


def test_a_transfer_longer_than_its_maximum_duration_is_cut(lab: World) -> None:
    lib = library(lab)
    lib.add(Chapter("1", [("1.png", page("png", "long", 600_000))]))
    clock = Clock()
    api = provider(
        lab,
        limits=BundleLimits(chunk_bytes=16_384, max_duration_s=100, rate_window_s=10_000),
        monotonic=clock,
    )
    download = api.open(
        user_id=user_id(lab, "mum"), scope=scope_of(lab, "mum"), edition_id=lab.seed.editions["ok"]
    )
    chunks = iter(download.chunks)
    next(chunks)
    clock.now = 101
    with pytest.raises(BundleGoneError):
        next(chunks)


def test_the_limits_of_the_lease_are_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        BundleLimits(send_timeout_s=0)
    with pytest.raises(ValueError, match="positive"):
        BundleLimits(max_duration_s=-1)


# -- a real client that goes away --------------------------------------------------------------


class Server:
    """The application behind a real socket, on a thread."""

    def __init__(self, app: Any) -> None:
        import uvicorn

        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> Server:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.server.started
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def port(self) -> int:
        return int(self.server.servers[0].sockets[0].getsockname()[1])


def _ask(port: int, path: str, headers: dict[str, str]) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    lines = [f"GET {path} HTTP/1.1", "Host: shelfhold.test", "Connection: close"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    return sock


def _status(sock: socket.socket) -> int:
    head = b""
    while b"\r\n\r\n" not in head:
        head += sock.recv(4096)
    head += sock.recv(2048) if head.split()[1] == b"403" else b""
    first = head.split(b"\r\n", 1)[0]
    assert first.startswith(b"HTTP/1.1 "), head[:300]
    status = int(first.split()[1])
    assert status in (200, 429), head[-700:]  # the reason of anything else, in the message
    return status


def test_a_real_client_that_drops_the_connection_gives_its_slot_back_at_once(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    from tests.integration.test_l48_opds import make_world

    plain = settings.model_copy(update={"device_allow_plain_http": True})  # a socket, not TLS
    with make_world(monkeypatch, plain) as lab:
        lib = library(lab)
        lib.add(Chapter("1", [("1.png", page("png", "wifi", 12_000_000))]))
        path = f"/opds/editions/{lab.seed.editions['ok']}/complete"
        with Server(lab.env.app) as server:
            first = _ask(server.port, path, lab.mum.headers)
            assert _status(first) == 200  # the block is on its way
            first.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            first.close()  # the Wi-Fi of the e-reader goes away: a reset, no goodbye
            deadline, status = time.monotonic() + 5, 0
            while time.monotonic() < deadline:
                second = _ask(server.port, path, lab.mum.headers)
                status = _status(second)
                second.close()
                if status == 200:
                    break
                assert status == 429, status  # the slot is still taken, nothing else
                time.sleep(0.05)
            assert status == 200, "the slot of the client that went away was not given back"


# -- the history of the parts and of the numbers -----------------------------------------------


def _versions(world: World) -> list[tuple[int, int, bool]]:
    v = bundle.bundle_version
    with world.seed.engine.connect() as c:
        rows = c.execute(
            sa.select(v.c.part_index, v.c.number, v.c.purged_at).order_by(
                v.c.part_index, v.c.number
            )
        ).all()
    return [(r.part_index, r.number, r.purged_at is not None) for r in rows]


def test_a_number_of_version_is_never_served_twice_even_after_a_boundary_goes(
    lab: World,
) -> None:
    lib = library(lab)
    items = [
        lib.add(Chapter(str(n), [(f"{p}.png", page("png", f"c{n}p{p}", 400)) for p in (1, 2)]))
        for n in range(1, 5)
    ]
    api = provider(lab, limits=BundleLimits(part_bytes=3_000))
    who, scope, edition = user_id(lab, "mum"), scope_of(lab, "mum"), lab.seed.editions["ok"]
    assert len(api.offers(user_id=who, scope=scope, edition_id=edition)) == 2
    old = api.open(user_id=who, scope=scope, edition_id=edition, part=2)
    drain(old)
    assert " v1" in old.filename
    with write_transaction(lab.seed.engine) as c:  # the chapter that opens part 2 is gone
        c.execute(sa.delete(export.export_item).where(export.export_item.c.id == items[2].item_id))
    again = api.open(user_id=who, scope=scope, edition_id=edition, part=2)
    drain(again)
    assert " v2" in again.filename  # not "v1" again: that number belongs to another file
    assert _versions(lab) == [(2, 1, True), (2, 2, False)]
    with pytest.raises(BundleGoneError):  # the first one answers 410, with its row and number kept
        api.open_version(user_id=who, scope=scope, version_id=old.version_id)


def test_a_version_restored_without_its_members_is_served_again_with_the_same_number(
    lab: World,
) -> None:
    """What an import gives back: the rows of the history, neither the members nor the alias."""
    lib = library(lab)
    for n in ("1", "2"):
        lib.add(Chapter.simple(n, count=2))
    api = provider(lab)
    who, scope, edition = user_id(lab, "mum"), scope_of(lab, "mum"), lab.seed.editions["ok"]
    first = api.open(user_id=who, scope=scope, edition_id=edition)
    body = drain(first)
    with write_transaction(lab.seed.engine) as c:
        c.execute(sa.delete(bundle.bundle_member))
        c.execute(sa.delete(export.document_alias))
    again = api.open(user_id=who, scope=scope, edition_id=edition)
    assert (again.version_id, again.filename, drain(again)) == (
        first.version_id,
        first.filename,
        body,
    )
    by_version = api.open_version(user_id=who, scope=scope, version_id=first.version_id)
    assert drain(by_version) == body  # members and alias are back
    with lab.seed.engine.connect() as c:
        assert (
            c.execute(sa.select(sa.func.count()).select_from(export.document_alias)).scalar() == 1
        )


def test_the_export_carries_the_history_and_an_import_gives_it_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    from tests.integration.test_l37_export_all import (
        ADMIN,
        export_to,
        fresh_instance,
        home,
        settings_in,
        shelve,
    )
    from tests.l50_env import seeded

    del home
    from shelfhold.ops.export_all import ExportAllOps

    base = settings_in(tmp_path)
    monkeypatch.setenv("SHELFHOLD_LIBRARY_DIR", str(base.library_dir))
    monkeypatch.setenv("SHELFHOLD_DISK_PAUSE_PERCENT", "0")
    monkeypatch.setenv("SHELFHOLD_DISK_PAUSE_GIB", "0")
    from datetime import UTC, datetime

    from shelfhold.identity.public import reader_scope
    from shelfhold.infrastructure.ids import new_ulid
    from shelfhold.infrastructure.schema import identity
    from shelfhold.infrastructure.secrets.keyring import Keyring
    from shelfhold.reading.bundle import SeriesBundles

    keyring = Keyring.generate()
    with seeded(base, tmp_path) as env:
        shelved = shelve(env)
        reader = new_ulid()
        with write_transaction(env.inst.engine) as c:
            c.execute(sa.insert(identity.user).values(id=reader, username="mum", role="member"))
        api = SeriesBundles(
            env.inst.engine, lambda: keyring, now=lambda: datetime(2026, 11, 15, tzinfo=UTC)
        )
        with env.inst.engine.connect() as c:
            scope = reader_scope(c, reader)
        download = api.open(user_id=reader, scope=scope, edition_id=shelved.kept.edition_id)
        drain(download)
        tables = (bundle.bundle_partition, bundle.bundle_version, export.document_alias)
        before = {
            t.name: sorted(map(tuple, env.rows(t))) for t in tables
        }  # the blocks' aliases, and those of the files the library exported (M4 L47)
        assert all(before.values()), before
        out = tmp_path / "out"
        export_to(env, out)
    import json

    for name in ("bundle_partition", "bundle_version", "document_alias"):
        rows = json.loads((out / "data" / f"{name}.json").read_text(encoding="utf-8"))
        assert rows, name
    members = (out / "data").glob("bundle_member*")
    assert not list(members)  # the fingerprints of the files stay behind

    fresh_home = settings_in(tmp_path / "fresh")
    with fresh_instance(fresh_home, tmp_path / "fresh") as target:
        ExportAllOps(target.comp).import_all(str(out), principal=ADMIN)
        after = {
            t.name: sorted(map(tuple, (r for r in _rows_of(target.engine, t)))) for t in tables
        }
    assert after["bundle_partition"] == before["bundle_partition"]
    assert after["bundle_version"] == before["bundle_version"]  # numbers, sizes, partial MD5
    carriers = {r[3] for r in before["bundle_partition"]}
    # every alias comes back, a block's included (M4 L47: nothing rebuilds the alias of a file
    # that was exported once and is up to date)
    assert {r[0] for r in after["document_alias"]} == {r[0] for r in before["document_alias"]}
    assert carriers <= {r[0] for r in after["document_alias"]}


def _rows_of(engine: sa.Engine, table: sa.Table) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(sa.select(table)))


# -- the carrier of the aliases is no volume of a range ----------------------------------------


def test_a_block_download_leaves_no_item_of_the_kind_the_exporter_reads(
    lab: World,
) -> None:
    lib = library(lab)
    lib.add(Chapter.simple("1"))
    complete(lab.mum, lab)
    with lab.seed.engine.connect() as c:
        kinds = sorted(c.execute(sa.select(export.export_item.c.kind).distinct()).scalars().all())
    assert kinds == ["opds_bundle", "unit"]  # never `bundle`, the nameless volume of a range


def test_the_nameless_volume_of_a_range_is_a_chapter_of_the_block(lab: World) -> None:
    lib = library(lab)
    lib.add(Chapter.simple("1", count=2))
    lib.add(Chapter.simple("2", count=2), kind="bundle", covers=2)
    lib.add(Chapter.simple("4", count=2))
    names = _names(complete(lab.mum, lab).content)
    assert [n.split("/")[0] for n in names[::2]] == ["00001 ch1", "00002 ch2", "00003 ch4"]


def _names(body: bytes) -> list[str]:
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        return [i.filename for i in archive.infolist()]


# -- an archive that lies on a CRC -------------------------------------------------------------


def test_a_chapter_that_lies_on_a_crc_is_left_out_once_and_said_so(lab: World) -> None:
    import struct
    import zipfile

    lib = library(lab)
    bad = lib.add(Chapter("1", [("1.png", page("png", "bad", 50_000))]))
    lib.add(Chapter.simple("2", count=2))
    data = bytearray(bad.path.read_bytes())
    with zipfile.ZipFile(bad.path) as archive:
        info = archive.infolist()[0]
    struct.pack_into("<I", data, info.header_offset + 14, info.CRC ^ 1)
    struct.pack_into("<I", data, data.find(b"PK\x01\x02") + 16, info.CRC ^ 1)
    bad.path.write_bytes(bytes(data))
    with write_transaction(lab.seed.engine) as c:
        c.execute(
            sa.update(export.export_record)
            .where(export.export_record.c.export_item_id == bad.item_id)
            .values(size=len(data), mtime_ns=bad.path.stat().st_mtime_ns)
        )
    first, second = complete(lab.mum, lab), complete(lab.mum, lab)
    assert first.status_code == second.status_code == 200
    assert first.content == second.content  # the same file, not a new version each time
    assert [n.split("/")[0] for n in _names(first.content)] == ["00001 ch2"] * 2
    with lab.seed.engine.connect() as c:
        excluded = c.execute(sa.select(bundle.bundle_version.c.excluded)).scalars().all()
    assert excluded == [{"unreadable": 1}]
    assert _versions(lab) == [(1, 1, False)]
