"""EF-EXP-022, EF-EXP-058 (M4 L09): an EPUB is dropped atomically into the ingestion folder of a
CWA drop box, and followed by its identifier once the ingester has taken it and the file is gone.

Real database, real files, the executor of L04 and the fan-out of L06: the destination describes
(`CwaSource` reads what was planned), the host serves the bytes of the verified derivative and
writes them. The ingester is `FakeIngest`: it takes whole EPUBs and removes them.
"""

import contextlib
import hashlib
import threading
import time

import pytest

from shelfhold.export.application.fan_out import EpubFile
from shelfhold.infrastructure.schema import export
from tests.cwa_env import add_book, bind_cwa, folder, names, records, rig
from tests.export_env import export_env
from tests.fakes.cwa import FakeIngest

LONG = b"synthetic page " * 60_000  # about 900 KiB: a write that takes more than one instant


@pytest.mark.req("EF-EXP-022")
def test_cwa_drop_box() -> None:
    with export_env() as env:
        store: dict[str, EpubFile] = {}
        books = [add_book(env, env.new_series("Synthetic Book"), n, store) for n in ("1", "2")]
        destination = bind_cwa(env)
        fan, _source = rig(env, store)
        outcomes = fan.run(env.shelf_id, "c1").outcomes
        mine = [o for o in outcomes if o.destination_id == destination]
        assert [(o.status, o.executed) for o in mine] == [("done", 2)]
        # the folder holds the two EPUBs, whole, under the name that carries the item
        assert names(env) == sorted(f"Synthetic Book [{b.item_id}].epub" for b in books)
        for book in books:
            written = folder(env) / f"Synthetic Book [{book.item_id}].epub"
            assert written.read_bytes() == book.data
        # nothing else was written: no temporary file, no staging leftover, no sidecar
        stray = [p for p in env.lib.library.rglob("*") if p.name.endswith((".tmp", ".part"))]
        assert stray == []
        ingest = FakeIngest(folder(env))
        assert sorted(i.data for i in ingest.scan()) == sorted(b.data for b in books)
        assert ingest.violations == []


@pytest.mark.req("EF-EXP-022", "EF-EXP-058")
def test_a_drop_is_atomic_no_partial_file_is_ever_visible() -> None:
    """A watcher lists the folder as fast as it can while the host drops a large EPUB: every name
    it ever sees is the final one, and whole (the file is written aside, then renamed in)."""
    with export_env() as env:
        store: dict[str, EpubFile] = {}
        book = add_book(env, env.new_series("Synthetic Book"), "1", store)
        big = book.data + LONG  # a zip with trailing bytes is still the bytes of that file
        path = env.lib.manga / "received" / f"{book.unit_id}.epub"
        path.write_bytes(big)
        store[book.unit_id] = EpubFile(
            str(path), "sha256:" + hashlib.sha256(big).hexdigest(), book.epub.content_id
        )
        bind_cwa(env)
        fan, _source = rig(env, store)
        seen: dict[str, set[int]] = {}
        stop = threading.Event()

        def watch() -> None:
            while not stop.is_set():
                for entry in folder(env).iterdir():
                    with contextlib.suppress(FileNotFoundError):
                        seen.setdefault(entry.name, set()).add(entry.stat().st_size)

        watcher = threading.Thread(target=watch)
        watcher.start()
        try:
            time.sleep(0.05)
            fan.run(env.shelf_id, "c1")
            time.sleep(0.05)
        finally:
            stop.set()
            watcher.join()
        final = f"Synthetic Book [{book.item_id}].epub"
        assert set(seen) == {final}  # never a temporary name, never a half-written file
        assert seen[final] == {len(big)}


@pytest.mark.req("EF-EXP-022", "EF-EXP-058")
def test_the_drop_is_followed_by_its_identifier_after_the_file_is_gone() -> None:
    with export_env() as env:
        store: dict[str, EpubFile] = {}
        first = add_book(env, env.new_series("Synthetic Book"), "1", store)
        destination = bind_cwa(env)
        fan, _source = rig(env, store)
        fan.run(env.shelf_id, "c1")
        (line,) = records(env, destination)
        # a drop box has no path to follow: the line is typed `idempotency_key`, with a key of its
        # own, and says the host wrote the file (EF-EXP-058)
        assert line.export_item_id == first.item_id and line.state == "written"
        assert (line.locator_type, line.rel_path, line.remote_id) == ("idempotency_key", None, None)
        assert line.idempotency_key and line.written_by_shelfhold_at is not None
        key = line.idempotency_key

        ingest = FakeIngest(folder(env))
        assert [i.data for i in ingest.scan()] == [first.data]  # the ingester took it: it is gone
        assert names(env) == []

        second = add_book(env, env.new_series("Another Book"), "1", store)
        for call in ("c2", "c3"):  # every later pass: the first book is not dropped again
            outcome = next(
                o for o in fan.run(env.shelf_id, call).outcomes if o.destination_id == destination
            )
            assert outcome.status == "done"
        assert names(env) == [f"Another Book [{second.item_id}].epub"]
        lines = {r.export_item_id: r for r in records(env, destination)}
        assert set(lines) == {first.item_id, second.item_id}
        # the first is still `written`: a missing file is how an ingested drop looks, not a loss
        assert lines[first.item_id].state == "written"
        assert lines[first.item_id].idempotency_key == key
        assert lines[second.item_id].idempotency_key != key
        with env.engine.connect() as connection:
            copies = connection.execute(
                export.export_record.select().where(export.export_record.c.state == "missing")
            ).all()
        assert copies == []


@pytest.mark.req("EF-EXP-022")
def test_a_book_that_is_in_the_folder_but_not_ingested_yet_is_not_dropped_twice() -> None:
    with export_env() as env:
        store: dict[str, EpubFile] = {}
        add_book(env, env.new_series("Synthetic Book"), "1", store)
        destination = bind_cwa(env)
        fan, _source = rig(env, store)
        fan.run(env.shelf_id, "c1")
        before = names(env)
        for call in ("c2", "c3"):
            outcome = next(
                o for o in fan.run(env.shelf_id, call).outcomes if o.destination_id == destination
            )
            assert (outcome.status, outcome.executed) == ("done", 0)
        assert names(env) == before and len(before) == 1


@pytest.mark.req("EF-EXP-022")
def test_a_drop_box_is_not_scanned_for_another_writer_and_has_no_sentinel() -> None:
    with export_env() as env:
        store: dict[str, EpubFile] = {}
        add_book(env, env.new_series("Synthetic Book"), "1", store)
        bind_cwa(env)
        fan, _source = rig(env, store)
        fan.run(env.shelf_id, "c1")
        # the ingester (or the owner) puts its own files in the folder: nothing here minds
        (folder(env) / "someone else.epub").write_bytes(b"PK\x03\x04 other")
        fan.run(env.shelf_id, "c2")
        assert "someone else.epub" in names(env)
        assert not [n for n in names(env) if "sentinel" in n.lower() or n.startswith(".")]
