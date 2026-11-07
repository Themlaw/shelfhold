"""Keep-copy mode (EF-BIB-027), declared canonical derivative (EF-BIB-006), `store/INDEX.jsonl`
(EF-INT-027) and the catch-up under the store budget (EF-BIB-042 (4)): real files, a real DB."""

import errno
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import coherence, export, platform, storage
from shelfhold.jobs.window import run_until
from shelfhold.library.public import (
    KeepCopier,
    PassthroughStore,
    SafeguardFailedError,
    StoreBudget,
    StoreIndex,
    complete_interrupted_keep,
    independent_copies,
    keep_candidates,
    plan_batch,
    verify_kept,
)
from tests.fakes_clock import ManualClock
from tests.library_env import JPEG, PNG, LibEnv, cbz_bytes, lib_env
from tests.null_tombstones import NONE_PURGED


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def keeper(env: LibEnv, *, extract: Any = None) -> KeepCopier:
    return KeepCopier(
        env.engine, env.fsops, env.locate, str(env.store), env.data,
        IntentJournal(env.engine, env.fsops), extract=extract,
    )  # fmt: skip


def prepare(env: LibEnv) -> None:
    """An instance row (the journal reads its restore epoch) and nothing else."""
    with env.engine.begin() as connection:
        connection.execute(
            sa.insert(platform.instance).values(instance_id=env.instance_id, jurisdiction="FR")
        )


def ingest(env: LibEnv, data: bytes, unit_id: str, variant_id: str, name: str) -> Path:
    dest = env.manga / "Dr Stone" / name
    PassthroughStore(env.engine, env.fsops, env.locate, env.data, tombstones=NONE_PURGED).ingest(
        unit_id=unit_id, variant_id=variant_id, staged=env.stage(data), dest=str(dest),
    )  # fmt: skip
    return dest


def shelf_with(env: LibEnv, unit_ids: list[str], *, mode: str = "keep") -> None:
    """A shelf in `mode`, bound to a destination, with an export record for each Unit's content."""
    target_id, shelf_id, destination_id = new_ulid(), new_ulid(), new_ulid()
    with env.engine.begin() as c:
        c.execute(
            sa.insert(export.export_target).values(
                id=target_id, kind="kavita", name="K", path_map=[]
            )
        )
        c.execute(
            sa.insert(export.shelf).values(id=shelf_id, name="S", phrase="p", retention_mode=mode)
        )
        c.execute(
            sa.insert(export.shelf_destination).values(
                id=destination_id, shelf_id=shelf_id, target_id=target_id, transport="tree",
                egress="foyer", template="t", template_origin="default@1",
            )
        )  # fmt: skip
        for unit_id in unit_ids:
            content_id = c.execute(
                sa.select(storage.content.c.id).where(storage.content.c.unit_id == unit_id)
            ).scalar_one()
            item_id = new_ulid()
            c.execute(
                sa.insert(export.export_item).values(
                    id=item_id, edition_id=env.chain["edition"], kind="unit",
                    content_id=content_id,
                )
            )  # fmt: skip
            c.execute(
                sa.insert(export.export_record).values(
                    shelf_destination_id=destination_id, export_item_id=item_id,
                    locator_type="none",
                )
            )  # fmt: skip


def two_units(env: LibEnv) -> list[tuple[str, Path, bytes]]:
    found = []
    for n, pages in enumerate(
        ((("001.png", PNG), ("002.jpg", JPEG)), (("001.png", PNG + b"2"), ("002.jpg", JPEG)))
    ):
        if n == 0:
            unit_id, variant_id = env.chain["unit"], env.chain["variant"]
        else:
            unit_id, variant_id = env.new_unit()
        data = cbz_bytes(pages)
        found.append(
            (unit_id, ingest(env, data, unit_id, variant_id, f"Dr Stone - {n + 1}.cbz"), data)
        )
    return found


@pytest.mark.req("EF-BIB-027")
def test_keep_mode_makes_independent_copy_distinct_inode() -> None:
    data = cbz_bytes()
    with lib_env() as env:
        prepare(env)
        unit = env.chain["unit"]
        library = ingest(env, data, unit, env.chain["variant"], "Dr Stone - 1.cbz")
        kept = keeper(env).keep(unit)
        (stored,) = env.files_under(env.store)
        assert kept.method in {"reflink", "copy"}  # a clone where the file system allows it
        assert stored.read_bytes() == data == library.read_bytes()
        assert stored.stat().st_ino != library.stat().st_ino  # never one inode under two names
        assert stored.stat().st_nlink == 1 and library.stat().st_nlink == 1
        with env.engine.connect() as c:
            row = c.execute(sa.select(storage.content)).one()
            assert (row.retention, row.store_provenance, row.path) == (
                "keep", "received", stored.relative_to(env.store).as_posix(),
            )  # fmt: skip
            assert independent_copies(c, unit) == 2  # /library and /store
            intent = c.execute(sa.select(coherence.fs_intent)).one()
            assert (intent.op, intent.state) == ("copy_cross_root", "done")


@pytest.mark.req("EF-BIB-027")
def test_keep_copy_never_links_and_falls_back_to_a_copy_without_reflink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_link(*_: Any, **__: Any) -> None:
        raise AssertionError("a kept copy must never be made with a link")

    def no_reflink(*_: Any, **__: Any) -> None:
        raise OSError(errno.EOPNOTSUPP, "no reflink here")

    monkeypatch.setattr(os, "link", refuse_link)
    monkeypatch.setattr("shelfhold.infrastructure.fsops.writer.fcntl.ioctl", no_reflink)
    with lib_env() as env:
        prepare(env)
        ingest(env, cbz_bytes(), env.chain["unit"], env.chain["variant"], "Dr Stone - 1.cbz")
        assert keeper(env).keep(env.chain["unit"]).method == "copy"
        (stored,) = env.files_under(env.store)
        assert stored.stat().st_nlink == 1


@pytest.mark.req("EF-BIB-027")
def test_a_rendered_library_file_is_not_copied_byte_for_byte() -> None:
    with lib_env() as env:
        prepare(env)
        dest = ingest(env, cbz_bytes(), env.chain["unit"], env.chain["variant"], "Dr Stone - 1.cbz")
        env.fsops.write_atomic(str(dest), cbz_bytes(comicinfo=b"<ComicInfo/>"))
        with pytest.raises(SafeguardFailedError) as raised:  # no safeguard to extract the original
            keeper(env).keep(env.chain["unit"])
        assert raised.value.reason == "library_differs_from_original"
        assert env.files_under(env.store) == []  # /store never holds a rendered file


@pytest.mark.req("EF-BIB-027")
def test_a_copy_interrupted_after_the_disk_is_completed_at_restart() -> None:
    data = cbz_bytes()
    with lib_env() as env:
        prepare(env)
        unit = env.chain["unit"]
        library = ingest(env, data, unit, env.chain["variant"], "Dr Stone - 1.cbz")
        with env.engine.connect() as c:
            content = c.execute(sa.select(storage.content)).one()
        target = f"{env.store}/units/{unit[:2]}/{unit}/{content.id}.cbz"
        journal = IntentJournal(env.engine, env.fsops)
        seen = env.fsops.observe(str(library))
        intent = journal.record(
            op="copy_cross_root", src=str(library), dst=target,
            expected_sha256=content.original_sha256, expected_file_id=seen[1] if seen else None,
        )  # fmt: skip
        journal.mark_applying(intent)
        env.fsops.copy_independent(str(library), target, parents=True)
        # the process dies here: the file is in /store, the database still says passthrough
        report = journal.resolve_open(
            on_done=lambda conn, i: complete_interrupted_keep(
                conn, env.fsops, env.locate, src=i.src, dst=i.dst or "", now=datetime.now(UTC)
            )
        )
        assert report.completed == [intent]
        with env.engine.connect() as c:
            assert c.execute(sa.select(storage.content.c.retention)).scalar_one() == "keep"
            assert independent_copies(c, unit) == 2


@pytest.mark.req("EF-BIB-006")
def test_declared_canonical_derivative_keeps_original_and_derived_sha256() -> None:
    scrambled = cbz_bytes((("001.png", PNG + b"scrambled"), ("002.jpg", JPEG)))
    derived = cbz_bytes((("001.png", PNG), ("002.jpg", JPEG)))
    with lib_env() as env:
        prepare(env)
        unit = env.chain["unit"]
        library = ingest(env, scrambled, unit, env.chain["variant"], "Dr Stone - 1.cbz")
        file = env.stage(derived, "derived.cbz")
        declared = keeper(env).declare_derivative(unit, file, "descrambler@v1")
        assert (declared.original_sha256, declared.derived_sha256) == (sha(scrambled), sha(derived))
        (stored,) = env.files_under(env.store)
        assert stored.read_bytes() == scrambled  # the bytes as received, in /store
        assert library.read_bytes() == derived  # the canonical derivative, in /library
        with env.engine.connect() as c:
            row = c.execute(sa.select(storage.content)).one()
            assert (row.original_sha256, row.derived_hash, row.derived_by) == (
                sha(scrambled), sha(derived), "descrambler@v1",
            )  # fmt: skip
            assert row.retention == "keep"
            replicas = c.execute(sa.select(storage.replica.c.location, storage.replica.c.file_hash))
            assert sorted(replicas.all()) == sorted(
                [("library", sha(derived)), ("store", sha(scrambled))]
            )
        again = keeper(env).declare_derivative  # a derivative is declared once
        with pytest.raises(SafeguardFailedError):
            again(unit, file, "descrambler@v1")


@pytest.mark.req("EF-BIB-027", "EF-BIB-045")
def test_health_counts_only_independent_copies() -> None:
    with lib_env() as env:
        prepare(env)
        unit = env.chain["unit"]
        library = ingest(env, cbz_bytes(), unit, env.chain["variant"], "Dr Stone - 1.cbz")
        keeper(env).keep(unit)
        (stored,) = env.files_under(env.store)
        with env.engine.connect() as c:
            assert independent_copies(c, unit) == 2
        assert verify_kept(env.engine, env.fsops, unit) == 1  # a distinct inode, the same bytes
        stored.unlink()  # the "kept copy" becomes a second name of the library file
        os.link(library, stored)
        assert verify_kept(env.engine, env.fsops, unit) == 0
        with env.engine.connect() as c:
            assert independent_copies(c, unit) == 1  # one inode under two names is one copy
        stored.unlink()
        stored.write_bytes(b"rotten")  # another inode, but not the bytes that were stored
        assert verify_kept(env.engine, env.fsops, unit) == 0
        with env.engine.connect() as c:
            assert independent_copies(c, unit) == 1


@pytest.mark.req("EF-BIB-027")
def test_a_reflink_clone_counts_as_an_independent_copy() -> None:
    from shelfhold.library.public import CopyFacts, is_independent

    original = CopyFacts(1, 10, "sha256:aa")
    clone = CopyFacts(1, 11, "sha256:aa")  # own inode, blocks shared copy-on-write
    link = CopyFacts(1, 10, "sha256:aa")
    assert is_independent(clone, stored_sha256="sha256:aa", siblings={(1, 10)})
    assert not is_independent(link, stored_sha256="sha256:aa", siblings={(original.dev, 10)})
    assert not is_independent(clone, stored_sha256="sha256:bb", siblings={(1, 10)})


@pytest.mark.req("EF-BIB-042")
def test_passthrough_to_keep_catchup_job_under_quota() -> None:
    with lib_env() as env:
        prepare(env)
        found = two_units(env)
        extra_id, extra_variant = env.new_unit()
        ingest(
            env,
            cbz_bytes((("001.png", PNG + b"3"), ("002.jpg", JPEG))),
            extra_id,
            extra_variant,
            "Dr Stone - 3.cbz",
        )
        shelf_with(env, [u for u, _, _ in found] + [extra_id])
        candidates = keep_candidates(env.engine)
        assert len(candidates) == 3
        size = max(c.size for c in candidates)
        room = StoreBudget(free_bytes=size * 2 + 1000, reserve_bytes=1000)
        first = plan_batch(candidates, room)  # two fit, one waits: the backlog
        assert (len(first.selected), first.backlog) == (2, 1)
        copier = keeper(env)
        done: list[str] = []

        def step(unit_id: str):
            return lambda: done.append(copier.keep(unit_id).unit_id)

        clock = ManualClock(datetime.now(UTC))
        steps = [step(c.unit_id) for c in first.selected]
        stopped = run_until(
            steps, deadline=clock.now().replace(year=2999), clock=clock, stop=lambda: len(done) >= 1
        )
        assert stopped.preempted and stopped.next_index == 1  # the window closed between two Units
        resumed = run_until(
            steps, deadline=clock.now().replace(year=2999), clock=clock, start=stopped.next_index
        )
        assert not resumed.preempted and len(done) == 2  # taken up where it stopped
        rest = keep_candidates(env.engine)  # the next night: only the backlog is left
        assert len(rest) == 1
        assert plan_batch(rest, StoreBudget(free_bytes=10**9, reserve_bytes=0)).backlog == 0
        copier.keep(rest[0].unit_id)
        assert keep_candidates(env.engine) == []
        assert len(env.files_under(env.store)) == 3


@pytest.mark.req("EF-BIB-042")
def test_a_shelf_in_passthrough_asks_for_no_catchup() -> None:
    with lib_env() as env:
        prepare(env)
        found = two_units(env)
        shelf_with(env, [u for u, _, _ in found], mode="passthrough")
        assert keep_candidates(env.engine) == []


class Gate:
    """A tombstone gate that refuses the content hashes it was given."""

    def __init__(self, *hashes: str) -> None:
        self.hashes = set(hashes)

    def check_content(self, hashes: Any = (), **_: Any) -> bool:
        return any(h.value in self.hashes for h in hashes)


@pytest.mark.req("EF-INT-027")
def test_store_index_regenerable_and_excludes_tombstoned() -> None:
    with lib_env() as env:
        prepare(env)
        found = two_units(env)
        copier = keeper(env)
        for unit_id, _, _ in found:
            copier.keep(unit_id)
        index_path = env.store / "INDEX.jsonl"
        index = StoreIndex(env.engine, env.fsops, str(env.store), NONE_PURGED)
        report = index.rewrite()
        assert (report.lines, report.tombstoned, report.rewritten) == (2, 0, True)
        first = index_path.read_bytes()
        lines = [json.loads(line) for line in first.decode().splitlines()]
        assert [line["unit"] for line in lines] == sorted(u for u, _, _ in found)
        assert set(lines[0]) == {"unit", "path", "title", "lang", "number", "sha256"}
        assert all((env.store / line["path"]).is_file() for line in lines)  # /store reads alone
        assert str(env.tmp) not in first.decode() and "/" not in lines[0]["sha256"]
        assert not index.rewrite().rewritten  # a quiet night rewrites nothing
        index_path.unlink()  # regenerable: lost, it comes back byte for byte
        assert index.rewrite().rewritten and index_path.read_bytes() == first
        banned = sha(found[0][2])
        gated = StoreIndex(env.engine, env.fsops, str(env.store), Gate(banned))
        report = gated.rewrite()
        assert (report.lines, report.tombstoned) == (1, 1)
        text = index_path.read_text()
        assert found[0][0] not in text and found[1][0] in text  # no line under a tombstone
