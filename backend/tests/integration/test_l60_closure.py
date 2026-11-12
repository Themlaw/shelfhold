"""L60: the objections of the closing adversary of M1a, proved on the real doors.

Each test goes through the production path of the thing it proves: the headless admin for the
service addresses, the follow handler for the range path, the export handler and the CLI for the
series names and the sentinel, the pairing and migration services for the download folder.
"""
# ruff: noqa: E501

from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
import sqlalchemy as sa

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.headless import InvalidConfigValue, parse_desired
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, export
from shelfhold.library.public import Received
from shelfhold.ops.library import LibraryOps
from shelfhold.plugin_host.adapters.meta.suwayomi.probe import HttpSuwayomiProbe
from shelfhold.sdk.refs import SeriesRef
from shelfhold.sdk.testing import FakeRangedConnectedService, fake_ranged_fetcher
from shelfhold.sdk.testing.fakes import FAKE_KEY
from tests.fakes.suwayomi.server import FakeSuwayomi, FakeSuwayomiFetcher
from tests.headless_env import SUWAYOMI_URL, headless
from tests.integration.test_schema_catalog import add_edition, add_work
from tests.l41_env import context, instance
from tests.l50_env import REMOTE_ROOT
from tests.ranged_env import volume_epub

# -- ENF-SEC-001: one rule for every service address ------------------------------------------------

_PUBLIC = ["http://8.8.8.8:4567", "http://suwayomi.example.org:4567", "http://1.1.1.1"]


@pytest.mark.req("ENF-SEC-001")
@pytest.mark.parametrize("url", _PUBLIC)
def test_suwayomi_address_outside_the_lan_over_http_is_refused_before_any_request(
    settings: Settings, url: str
) -> None:
    """The POST that reads the authentication mode must not leave for a public address."""
    server = FakeSuwayomi(auth_mode="none")
    probe = HttpSuwayomiProbe(lambda _url: FakeSuwayomiFetcher(server, SUWAYOMI_URL))  # type: ignore[arg-type,return-value]
    with headless(settings, suwayomi=probe) as env:
        with pytest.raises(InvalidConfigValue):
            env.admin.source_add_suwayomi(url=url, secret=None)
        assert server.requests == []  # nothing was sent
        assert env.count("source_config") == 0


@pytest.mark.req("ENF-SEC-001")
def test_suwayomi_address_of_the_household_and_https_are_accepted(settings: Settings) -> None:
    server = FakeSuwayomi(auth_mode="none")
    probe = HttpSuwayomiProbe(lambda url: FakeSuwayomiFetcher(server, str(url).rstrip("/")))  # type: ignore[arg-type,return-value]
    with headless(settings, suwayomi=probe) as env:
        for url in (
            "http://suwayomi.internal:4567",
            "http://192.168.1.20:4567",
            "https://suwayomi.example.org",
        ):
            assert env.admin.source_add_suwayomi(url=url, secret=None).auth_mode == "none"
        assert env.count("source_config") == 1


@pytest.mark.req("ENF-SEC-001")
def test_declarative_file_applies_the_same_rule_to_a_suwayomi_address(
    settings: Settings, tmp_path: Path
) -> None:
    text = (
        'schema_version = 1\n\n[source]\ncheck_mode = "always_on"\n\n'
        '[[sources]]\nkind = "suwayomi"\nurl = "http://8.8.8.8:4567"\n'
    )
    with headless(settings) as env:
        desired = parse_desired(text, base_dir=tmp_path)
        with pytest.raises(InvalidConfigValue):
            env.admin.apply(desired, dry_run=True)
        assert env.suwayomi.calls == []


# -- EF-SRC-034: the range path of the follow cycle is wired in production -------------------------


class _Library(FakeRangedConnectedService):
    async def followed_series(self, f: Any) -> list[SeriesRef]:
        return [self._series()]


class _Guarded:
    async def aclose(self) -> None:
        return None


@pytest.mark.req("EF-SRC-034", "EF-SRC-032")
def test_follow_handler_keeps_the_file_of_a_range_it_asked_for(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`follow_handler` builds the cycle: with a `ranged_content` + `server_queue` service the
    volumes are requested as ranges, and the host keeps each file under the item that covers it."""

    class Pools:
        slots = None
        submitted: ClassVar[list[str]] = []

        def submit(self, kind: str, **_: object) -> None:
            self.submitted.append(kind)

    @asynccontextmanager
    async def receive(asset: Any) -> Any:
        staged = tmp_path / "staged" / asset.filename
        staged.parent.mkdir(exist_ok=True)
        staged.write_bytes(volume_epub(1))  # a ranged file is an EPUB container (M2 L25)
        import hashlib

        digest = "sha256:" + hashlib.sha256(staged.read_bytes()).hexdigest()
        yield Received(path=staged, size=staged.stat().st_size, sha256=digest)

    with instance(settings, tmp_path) as inst:
        inst.configure()
        link = SimpleNamespace(
            service=_Library(), fetcher=fake_ranged_fetcher(), guarded=_Guarded(), url="https://x"
        )
        monkeypatch.setattr(inst.comp, "suwayomi", lambda: link)
        monkeypatch.setattr(rj, "bounded_receiver", lambda *_a, **_k: receive)
        with inst.engine.begin() as connection:
            work_id = add_work(connection)
            edition_id = add_edition(connection, work_id)
            item_id, binding_id = new_ulid(), new_ulid()
            connection.execute(
                sa.insert(acquisition.source_item).values(
                    id=item_id, plugin_id=FAKE_KEY, source_key=FAKE_KEY,
                    remote_id="series/fake-novel",
                )
            )  # fmt: skip
            connection.execute(
                sa.insert(acquisition.source_binding).values(
                    id=binding_id, work_id=work_id, edition_id=edition_id,
                    source_item_id=item_id, priority=1, active=True,
                )
            )  # fmt: skip
            connection.execute(
                sa.insert(acquisition.series_follow).values(edition_id=edition_id, work_id=work_id)
            )
        ranged = settings.data_dir / "ranged"
        ranged.mkdir(exist_ok=True)
        stale, fresh = ranged / f"{new_ulid()}.epub", ranged / f"{new_ulid()}.epub"
        for orphan in (stale, fresh):  # written, then the transaction did not commit
            orphan.write_bytes(b"orphan")
        os.utime(stale, (time.time() - 7200, time.time() - 7200))  # old enough to be abandoned
        for _ in range(5):  # discover, queued, downloading, ready
            rj.follow_handler(inst.comp, Pools())(context("follow.cycle"))  # type: ignore[arg-type]
        with inst.engine.connect() as connection:
            items = connection.execute(sa.select(export.export_item.c.id)).scalars().all()
            covered = connection.execute(
                sa.select(sa.func.count()).select_from(export.unit_coverage)
            ).scalar_one()
        assert not stale.exists()  # swept by the cycle
        assert fresh.exists()  # too young: its transaction may still be open
        fresh.unlink()
        kept = sorted(p.name for p in (settings.data_dir / "ranged").iterdir())
        assert len(items) == 2 and covered == 10  # Vol.1 (8 units) and the rolling Vol.2 (2)
        assert sorted(Path(n).stem for n in kept) == sorted(items)  # one file per item, by id
        assert all(n.endswith(".epub") for n in kept)
        assert [p for p in (settings.data_dir / "ranged").iterdir() if p.name.startswith(".")] == []


# -- the roots are read when an exit runs (series resume, target recheck) ---------------------------


@pytest.mark.req("EF-EXP-043", "EF-EXP-054", "EF-INT-023")
def test_exits_see_the_roots_registered_after_the_services_were_built(
    settings: Settings, tmp_path: Path
) -> None:
    """`target add` registers a root while the server runs: the services opened before it must
    resolve paths under that root, or `series resume` ends in `OutsidePathMapError`."""
    from shelfhold.ops.wiring import open_ops

    with instance(settings, tmp_path) as inst:
        ops = open_ops(settings, engine=inst.engine, composition=inst.comp)
        assert ops._fsops is None  # no root yet: nothing to mount
        inst.configure()  # `target add` after the services exist
        fsops = ops._fsops
        assert fsops is not None
        assert fsops.observe(str(inst.library / "Kavita" / "none.cbz")) is None  # inside the map
        again = ops._fsops
        assert again is not None and again is not fsops  # re-read at each use, not frozen


# -- EF-EXP-029: the sentinel gates the scans too ---------------------------------------------------

SENTINEL = ".shelfhold-mount-id"


def _sentinel_of(inst: Any) -> Path:
    """The sentinel of the root that holds the Kavita folder of the instance."""
    with inst.engine.connect() as connection:
        roots = connection.execute(sa.text("SELECT path_entry FROM physical_root")).scalars().all()
    folder = str(inst.library / "Kavita")
    (root,) = [Path(r) for r in roots if folder.startswith(r.rstrip("/") + "/")]
    return root / SENTINEL


@pytest.mark.req("EF-EXP-029", "EF-EXP-034")
def test_export_job_sends_no_scan_once_the_sentinel_is_gone(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """The mount goes away between the write and the debounced scan: Kavita would read the empty
    folder as a deletion. `flush_scans` asks the sentinel, and sends nothing."""
    from datetime import timedelta

    from shelfhold.export.domain.scan_debounce import ScanDebouncer
    from shelfhold.export.targets.kavita.client import KavitaClient
    from tests.l54_env import bench54

    init = ScanDebouncer.__init__
    monkeypatch.setattr(  # the quiet period of the debounce is 30 s of the real clock
        ScanDebouncer, "__init__", lambda self, clock, quiet=None: init(self, clock, timedelta(0))
    )
    with bench54(monkeypatch, settings, tmp_path) as b:
        kavita = b.env.inst.kavita
        sentinel = _sentinel_of(b.env.inst)
        kept = sentinel.read_bytes()
        series = b.env.new_series("Alpha")
        b.env.add_chapter(series, "1")
        b.export()
        assert kavita.scans, "control: with its sentinel the folder is scanned"
        sent = len(kavita.scans)

        flush = KavitaClient.flush_scans

        async def unmounted(self: Any, *args: Any, **kwargs: Any) -> list[str]:
            sentinel.unlink()  # the volume is gone when the quiet period ends
            return await flush(self, *args, **kwargs)

        monkeypatch.setattr(KavitaClient, "flush_scans", unmounted)
        b.env.add_chapter(series, "2")
        b.export()
        assert len(kavita.scans) == sent  # not one scan more
        sentinel.write_bytes(kept)


@pytest.mark.req("EF-EXP-029", "EF-EXP-034")
def test_arrival_poll_does_not_rescan_a_folder_whose_sentinel_is_gone() -> None:
    from tests.export_env import export_env
    from tests.integration.test_kavita_reinstall_audit import _first_scan, _poller
    from tests.kavita_export_env import wire

    with export_env() as env:
        w = wire(env)
        (rel,) = w.written()
        folder = _first_scan(w, rel)
        w.fake.never_indexed.add(folder)
        sentinel = env.lib.library / SENTINEL
        kept = sentinel.read_bytes()
        poller = _poller(w, fsops=lambda: env.lib.fsops)
        env.clock.advance(minutes=16)  # half the delay: the second scan is due
        sentinel.unlink()
        assert w.fake.scans == [folder]
        assert _run(poller.poll(w.client, env.destination_id)).rescanned == []
        assert w.fake.scans == [folder]  # no scan: the mount is not there
        sentinel.write_bytes(kept)
        assert _run(poller.poll(w.client, env.destination_id)).rescanned == [folder]
        assert w.fake.scans == [folder, folder]


def _run(coroutine: Any) -> Any:
    import asyncio

    return asyncio.run(coroutine)


# -- `mount.sentinel_missing` is a state `status` shows, with its way out ---------------------------


@pytest.mark.req("EF-EXP-029", "EF-INT-023")
def test_status_lists_the_missing_sentinel_with_its_command_and_recheck_leaves_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    from tests.l53_env import status_json, waiting_states
    from tests.l54_env import bench54

    with bench54(monkeypatch, settings, tmp_path) as b:
        sentinel = _sentinel_of(b.env.inst)
        kept = sentinel.read_bytes()
        series = b.env.new_series("Alpha")
        b.env.add_chapter(series, "1")
        sentinel.unlink()
        b.export()  # the export finds the mount without its sentinel
        report = status_json(monkeypatch, settings, capsys)
        (item,) = waiting_states(report)["mount_sentinel_missing"]
        target_id = (
            b.env.inst.engine.connect()
            .execute(sa.text("SELECT id FROM export_target"))
            .scalar_one()
        )
        assert item["command"] == f"shelfhold target recheck {target_id}"
        assert item["subject"] == b.env.destination_id and item["reason"]
        capsys.readouterr()
        assert b.cli("target", "recheck", target_id) != 0  # the mount is still wrong: it says so
        assert "mount_sentinel_missing" in waiting_states(
            status_json(monkeypatch, settings, capsys)
        )
        sentinel.write_bytes(kept)  # the administrator mounted the right volume
        capsys.readouterr()
        assert b.cli("target", "recheck", target_id) == 0
        assert "mount_sentinel_missing" not in waiting_states(
            status_json(monkeypatch, settings, capsys)
        )


# -- EF-EXP-032: a series name taken in the library waits for the administrator ---------------------


@pytest.mark.req("EF-EXP-032", "EF-INT-023")
def test_name_collision_is_listed_by_status_and_left_by_set_series_name(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Two Works of the same title, one `en`, one `fr`: the export blocks (no silent skip), `status`
    names the Edition and the command, the command gives the name, the export goes on."""
    from tests.l53_env import status_json, waiting_states
    from tests.l54_env import bench54

    with bench54(monkeypatch, settings, tmp_path) as b:
        english = b.env.new_series("Monster", lang="en")
        french = b.env.new_series("Monster", lang="fr")
        for series in (english, french):
            b.env.add_chapter(series, "1")
        b.export()
        assert not any(f.suffix == ".cbz" for f in (b.env.inst.library / "Kavita").rglob("*"))
        report = status_json(monkeypatch, settings, capsys)
        listed = {i["subject"]: i for i in waiting_states(report)["series_name_collision"]}
        assert set(listed) == {english.edition_id, french.edition_id}
        item = listed[english.edition_id]
        assert item["command"] == f'shelfhold edition set-series-name {english.edition_id} "<name>"'
        assert "Monster" in item["reason"]

        capsys.readouterr()
        assert b.cli("edition", "set-series-name", english.edition_id, "Monster (en)") == 0
        assert "Monster (en)" in capsys.readouterr().out
        # the other Edition no longer collides with anyone: the export goes on for both
        b.export()
        folders = sorted(p.name for p in (b.env.inst.library / "Kavita").iterdir() if p.is_dir())
        assert "Monster" in folders and "Monster (en)" in folders
        left = waiting_states(status_json(monkeypatch, settings, capsys))
        assert "series_name_collision" not in left
        # the name is refused when the library already has it, and when nothing waits
        assert b.cli("edition", "set-series-name", french.edition_id, "Monster (en)") != 0
        assert b.cli("edition", "set-series-name", english.edition_id, "Other") != 0


# -- EF-EXP-044: the download folder of Suwayomi is a signal of the switch-over ---------------------


@pytest.mark.req("EF-EXP-044")
def test_download_folder_read_from_suwayomi_is_a_signal_of_the_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    from shelfhold.ops.wiring import open_ops
    from tests.l54_env import SERIES, bench54, comic

    with bench54(monkeypatch, settings, tmp_path) as b:
        b.kavita_series(SERIES, [f"{SERIES} - Ch.1.cbz"], info=comic(SERIES, number="1"))
        assert b.adopt(capsys) == 0
        inst = b.env.inst
        ops = open_ops(settings, engine=inst.engine, composition=inst.comp)
        destination = b.env.destination_id
        assert ops.library.migration_report(destination)["detected"] is False  # nothing feeds it
        inst.suwayomi.state["downloads_path"] = str(inst.library / "Kavita")  # Suwayomi writes here
        report = ops.library.migration_report(destination)
        assert report["detected"] is True
        assert [tuple(f["signals"]) for f in report["series"]] == [("download_dir",)]
        # confirmation is tied to what was seen, then the shelf is clear
        ops.library.confirm_migration(destination_id=destination, fingerprint=report["fingerprint"])
        assert ops.library.migration_report(destination)["confirmed"] is True


# -- EF-EXP-032: a series of another source bears its name in Kavita too (review) -------------------


@pytest.mark.req("EF-EXP-032", "EF-INT-023")
def test_name_taken_by_a_foreign_kavita_series_waits_and_is_refused_by_the_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    from tests.l53_env import status_json, waiting_states
    from tests.l54_env import bench54

    with bench54(monkeypatch, settings, tmp_path) as b:
        # a series Kavita holds from elsewhere: Shelfhold has no export_series for it
        b.env.inst.kavita.add_series(
            "Alpha", f"{REMOTE_ROOT}/elsewhere/Alpha", [f"{REMOTE_ROOT}/elsewhere/Alpha/1.cbz"],
            library_id=int(b.library_ref),
        )  # fmt: skip
        series = b.env.new_series("Alpha")
        b.env.add_chapter(series, "1")
        b.export()
        assert not any(f.suffix == ".cbz" for f in (b.env.inst.library / "Kavita").rglob("*"))
        (item,) = waiting_states(status_json(monkeypatch, settings, capsys))[
            "series_name_collision"
        ]
        assert item["subject"] == series.edition_id
        assert item["command"] == f'shelfhold edition set-series-name {series.edition_id} "<name>"'
        capsys.readouterr()
        assert b.cli("edition", "set-series-name", series.edition_id, "alpha") != 0  # taken there
        assert b.cli("edition", "set-series-name", series.edition_id, "Alpha (mine)") == 0
        b.export()
        assert (b.env.inst.library / "Kavita" / "Alpha (mine)").is_dir()


# -- ENF-SEC-001: the URL rule resolves names and sees through transition addresses (review) --------


def _resolves(monkeypatch: pytest.MonkeyPatch, table: dict[str, list[str]]) -> None:
    import shelfhold.headless.urls as urls

    monkeypatch.setattr(urls, "_resolver", lambda: lambda host: table.get(host, []))


@pytest.mark.req("ENF-SEC-001")
@pytest.mark.parametrize("name", ["suwayomi.lan", "suwayomi", "box.local"])
def test_http_to_a_local_looking_name_that_resolves_to_a_public_address_is_refused(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    from shelfhold.headless.urls import validate_target_url

    _resolves(monkeypatch, {name: ["192.168.1.9", "93.184.216.34"]})  # one global answer is enough
    with pytest.raises(ValueError, match="https"):
        validate_target_url(f"http://{name}:4567")
    assert validate_target_url(f"https://{name}:4567")  # https is accepted whatever it resolves to
    _resolves(monkeypatch, {name: ["192.168.1.9"]})
    assert validate_target_url(f"http://{name}:4567")


@pytest.mark.req("ENF-SEC-001")
@pytest.mark.parametrize(
    "address",
    [
        "2002:0808:0808::1",
        "64:ff9b::8.8.8.8",
        "2001:0:4136:e378:8000:63bf:f7f7:f7f7",
        "::ffff:8.8.8.8",
    ],
)  # 6to4, NAT64, Teredo (client 8.8.8.8 once complemented), mapped
def test_transition_addresses_carrying_a_public_ipv4_are_public_for_the_rule_and_the_fetcher(
    address: str,
) -> None:
    import ipaddress

    from shelfhold.headless.urls import validate_target_url
    from shelfhold.infrastructure.fetcher.guard import lan_block_reason

    with pytest.raises(ValueError, match="public"):
        validate_target_url(f"http://[{address}]:4567")
    assert lan_block_reason(ipaddress.ip_address(address)) is not None  # a declared LAN target too


@pytest.mark.req("ENF-SEC-001")
def test_transition_address_carrying_a_private_ipv4_stays_a_lan_address() -> None:
    import ipaddress

    from shelfhold.infrastructure.fetcher.guard import lan_block_reason

    assert lan_block_reason(ipaddress.ip_address("64:ff9b::192.168.1.9")) is None


@pytest.mark.req("ENF-SEC-001", "EF-INT-023")
def test_stored_suwayomi_address_is_revalidated_and_status_says_when_it_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    from tests.l53_env import status_json

    with instance(settings, tmp_path) as inst:
        inst.configure(suwayomi=True)
        assert inst.comp.suwayomi() is not None
        with inst.engine.begin() as connection:  # edited behind the facade
            connection.execute(
                sa.text("UPDATE source_config SET settings = :s WHERE plugin_id = 'suwayomi'"),
                {"s": '{"base_url": "http://8.8.8.8:4567"}'},
            )
        assert inst.comp.suwayomi() is None  # no service, so no request
        report = status_json(monkeypatch, settings, capsys)
        (refused,) = report["service_urls"]
        assert refused["service"] == "suwayomi" and "8.8.8.8" in refused["url"]
        assert "public" in refused["reason"]


# -- EF-EXP-032: what an administrator may name a series (review) ----------------------------------

_BAD_NAMES = [
    "A\x00B", "A\tB", "A\nB", "A‮B", "A⁦B", "A/B", "A\\B", ".", "..", ".shelfhold",
    "CON", "nul", "Com1", "LPT9.txt", "aux.v2",
]  # fmt: skip


@pytest.mark.req("EF-EXP-032")
@pytest.mark.parametrize("name", _BAD_NAMES)
def test_series_name_that_cannot_be_a_folder_is_refused_and_the_edition_keeps_waiting(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
    name: str,
) -> None:
    from tests.l53_env import status_json, waiting_states
    from tests.l54_env import bench54

    with bench54(monkeypatch, settings, tmp_path) as b:
        first = b.env.new_series("Monster", lang="en")
        second = b.env.new_series("Monster", lang="fr")
        for series in (first, second):
            b.env.add_chapter(series, "1")
        b.export()
        capsys.readouterr()
        assert b.cli("edition", "set-series-name", first.edition_id, name) != 0
        refusal = capsys.readouterr().err
        assert refusal.strip(), "the refusal says why"
        assert first.edition_id in {
            i["subject"]
            for i in waiting_states(status_json(monkeypatch, settings, capsys))[
                "series_name_collision"
            ]
        }
        assert not list((b.env.inst.library / "Kavita").glob("*/"))  # nothing was created


# -- EF-EXP-029: the control before an unmount does not scan an unmounted library (review) ----------


@pytest.mark.req("EF-EXP-029", "EF-EXP-051")
def test_repatriation_check_sends_no_scan_when_the_sentinel_of_the_library_is_gone(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    from tests.l56_env import repatriation_bench

    with repatriation_bench(monkeypatch, capsys, settings, tmp_path) as r:
        fingerprint = r.fingerprint(capsys)
        assert r.cli("--confirm", fingerprint) == 0
        r.unmount()
        fake = r.b.env.inst.kavita
        fake.scans.clear()
        sentinel = _sentinel_of(r.b.env.inst)
        kept = sentinel.read_bytes()
        sentinel.unlink()
        capsys.readouterr()
        assert r.cli("--check") == 3  # nothing is verified, so nothing is declared safe
        assert "sentinelle" in capsys.readouterr().err
        assert fake.scans == []  # Kavita was not asked to read an unmounted folder
        sentinel.write_bytes(kept)
        assert r.cli("--check") == 0
        assert fake.scans


@pytest.mark.req("EF-EXP-029", "EF-EXP-060")
def test_scan_guard_of_a_dedicated_adopt_ro_mount_is_its_presence_not_a_sentinel(
    tmp_path: Path,
) -> None:
    """01 gives `adopt_ro` no sentinel (it is never written): a scan is allowed while the mount is
    there and refused as a blocked root when it is not. The writable root next to it keeps its
    sentinel: without it, no scan."""
    import shutil

    from shelfhold.infrastructure.fsops.errors import RootBlockedError
    from tests.fsops_env import make_env

    env = make_env(tmp_path)
    env.fsops.require_scan_allowed(str(env.upstream / "Series"))  # mounted, no sentinel: allowed
    env.fsops.require_scan_allowed(str(env.manga / "Series"))  # its sentinel is there
    (env.library / ".shelfhold-mount-id").unlink()
    with pytest.raises(RootBlockedError):
        env.fsops.require_scan_allowed(str(env.manga / "Series"))
    shutil.rmtree(env.upstream)  # the read-only volume is gone
    with pytest.raises(RootBlockedError):
        env.fsops.require_scan_allowed(str(env.upstream / "Series"))


# -- EF-EXP-044: a download folder that cannot be compared is said, not silent (review) --------------


@pytest.mark.req("EF-EXP-044", "EF-INT-023")
@pytest.mark.parametrize(
    "reported", ["", "relative/dir", "/home/suwayomi/.local/share/Tachidesk/downloads"]
)
def test_download_folder_of_suwayomi_that_cannot_be_compared_is_listed_as_unverifiable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
    reported: str,
) -> None:
    from shelfhold.ops.wiring import open_ops
    from tests.l53_env import status_json, status_text
    from tests.l54_env import SERIES, bench54, comic

    with bench54(monkeypatch, settings, tmp_path) as b:
        b.kavita_series(SERIES, [f"{SERIES} - Ch.1.cbz"], info=comic(SERIES, number="1"))
        assert b.adopt(capsys) == 0
        inst = b.env.inst
        ops = open_ops(settings, engine=inst.engine, composition=inst.comp)
        inst.suwayomi.state["downloads_path"] = reported
        report = ops.library.migration_report(b.env.destination_id)
        assert report["detected"] is False  # nothing could be compared: not "not shared"
        (listed,) = status_json(monkeypatch, settings, capsys)["unverifiable_dirs"]
        assert listed["destination"] == b.env.destination_id and listed["path"] == reported
        assert "non vérifiable" in status_text(monkeypatch, settings, capsys)
        # the folder becomes comparable: the mark goes, the signal is raised
        inst.suwayomi.state["downloads_path"] = str(inst.library / "Kavita")
        assert ops.library.migration_report(b.env.destination_id)["detected"] is True
        assert status_json(monkeypatch, settings, capsys)["unverifiable_dirs"] == []


# -- EF-EXP-056: the detachment removes what Shelfhold put, and only that (review) -------------------


@pytest.mark.req("EF-EXP-056", "EF-INT-023")
def test_taking_hold_records_shelfhold_s_exclusion_and_detach_leaves_the_administrator_s(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Through the commands: adopt, pair (the snapshot), the category named `Shelfhold` that holds
    the series out of Suwayomi's updates and another one of the administrator's, the next command
    that takes hold of the series (which writes the record), then `detach --restore-upstream`."""
    from tests.l54_env import SERIES, bench54, comic

    with bench54(monkeypatch, settings, tmp_path) as b:
        b.kavita_series(SERIES, [f"{SERIES} - Ch.1.cbz"], info=comic(SERIES, number="1"))
        assert b.adopt(capsys) == 0
        series_id = b.series_id(SERIES)
        b.upstream({n: f"Chapter {n}" for n in range(1, 4)})
        server = b.env.inst.suwayomi
        reading = server.add_category("Reading")
        server.assign("/series-alpha", reading)
        pair = ("adopt", "pair", "--series", series_id, "--source-key", "suwayomi:7001",
                "--remote-id", "/series-alpha", "--follow-from", "2")  # fmt: skip
        capsys.readouterr()
        assert b.cli(*pair) == 0  # the snapshot is taken here
        assert b.rows("SELECT 1 FROM upstream_snapshot")  # the snapshot is really taken
        mine = server.add_category("MyDropped", include_in_update="EXCLUDE")
        held = server.add_category("Shelfhold", include_in_update="EXCLUDE")
        server.assign("/series-alpha", mine)
        server.assign("/series-alpha", held)
        capsys.readouterr()
        LibraryOps(b.env.inst.comp)._snapshot()  # the next command that takes hold: it records
        (row,) = b.rows("SELECT payload FROM upstream_snapshot")
        assert json.loads(row["payload"])["shelfhold_excluded"] == [held]
        capsys.readouterr()
        assert b.cli("detach", "--restore-upstream") == 0
        out = capsys.readouterr().out
        assert server.categories_of("/series-alpha") == [reading, mine]
        assert "MyDropped" in out  # the one that stays is said
