"""#305 (M4 L06b, the bytes of the fan-out): the holes found by the adversarial review, one red PoC
each (`xfail(strict=True)`). Each test says what should hold and fails today for the reason it
gives; `strict` reports the day it passes.
"""

import os
import threading
from pathlib import Path

import pytest

from shelfhold.export.application.fan_out import ContentNeedsDownloadError, HostContentStore
from shelfhold.export.application.plan_executor import PlanRefusedError
from tests.byte_source_env import (
    add_format,
    drop_library_copy,
    other_target,
    put_destination_copy,
    unit_with_item,
)
from tests.export_env import export_env
from tests.fan_out_env import bind
from tests.integration.test_fan_out_bytes import Registry, _host_pass, caps, ref


@pytest.mark.req("EF-BIB-045")
@pytest.mark.xfail(strict=True, reason="#305 M1: a destination file is opened and read unbounded")
def test_a_destination_file_replaced_by_a_fifo_does_not_hang_the_pass() -> None:
    """A destination copy is a file a third party may change (`mutable_by_third_party`). The host
    opens it by name with `open()` (no `O_NOFOLLOW`, no `O_NONBLOCK`, not through the confined
    facade) and reads it whole before it hashes it: a FIFO (or a link to one) blocks the pass for
    good, and a link to `/dev/zero` or a huge file fills the memory. The size the registry holds is
    known before the read; a file that is not a regular file of that size is no source."""
    with export_env() as env:
        series = env.new_series()
        unit = unit_with_item(env, series)
        host = HostContentStore(env.engine, capabilities=Registry(env, caps("fetch")))
        written = put_destination_copy(env, env.destination_id, unit, unit.data)
        drop_library_copy(env, unit)
        written.unlink()
        os.mkfifo(written)  # what the server of the tree left at the path of the copy

        outcome: dict[str, object] = {}

        def read() -> None:
            try:
                outcome["data"] = host.read(env.destination_id, ref(unit.item_id))
            except (ContentNeedsDownloadError, PlanRefusedError) as error:
                outcome["error"] = error

        worker = threading.Thread(target=read, daemon=True)
        worker.start()
        worker.join(3)
        hung = worker.is_alive()
        if hung:  # let the reader go: a writer opens the FIFO and closes it (EOF)
            fd = os.open(written, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
            worker.join(5)
        assert not hung, "the read of a destination copy blocks on a FIFO"
        assert isinstance(outcome.get("error"), ContentNeedsDownloadError)


@pytest.mark.req("EF-BIB-051", "EF-EXP-046")
@pytest.mark.xfail(strict=True, reason="#305 M2: the EPUB served is not the one that was verified")
def test_the_epub_served_is_the_derivative_that_was_verified(tmp_path: Path) -> None:
    """`servable_epub` checks the file at a path (payload, neutralizer) and hashes it;
    `_epub_path_of` keeps the path only, and `HostContentStore._read_epub` reads whatever is at
    that path afterwards, with no hash. A restore, an adoption or a repair that puts the original
    back at that path between the check and the read (the case the docstring of `servable_epub`
    names) sends the original EPUB, unneutralized, to the server. The race is made deterministic
    here: the callable stands for `servable_epub` and the original lands right after it."""
    with export_env() as env:
        series = env.new_series()
        unit = unit_with_item(env, series)
        add_format(env, unit, "epub", b"PK epub derivative")
        derivative, original = b"PK clean derivative", b"PK original <script>evil()</script>"
        path = tmp_path / "chapter.epub"
        path.write_bytes(derivative)

        def epub_path(_unit_id: str) -> str:
            checked = str(path)  # what `servable_epub` verified
            path.write_bytes(original)  # a repair puts the original back at the same path
            return checked

        host = HostContentStore(
            env.engine, capabilities=Registry(env, caps("fetch", ("epub",))), epub_path=epub_path
        )
        try:
            served = host.read(env.destination_id, ref(unit.item_id))
        except (ContentNeedsDownloadError, PlanRefusedError):
            served = None
        assert served != original


@pytest.mark.req("EF-BIB-045")
@pytest.mark.xfail(strict=True, reason="#305 m1: readback is read from another binding's server")
def test_a_copy_on_a_server_that_cannot_read_back_is_no_source_through_another_binding() -> None:
    """`byte_sources` joins the `destination` replica to `export_record` on the export item only,
    not on the tree the file lives in. A file written in the tree of a server with `readback: none`
    is listed once per binding of the shelf whose line holds the archive's hash, each time with
    that binding's server: a second server that can read back vouches for a file it never held."""
    with export_env() as env:
        series = env.new_series()
        unit = unit_with_item(env, series)
        registry = Registry(env, caps("none"))  # the server of the first binding
        second = bind(env, "Second", target_id=other_target(env, caps("fetch"), registry.by_target))
        put_destination_copy(env, second, unit, unit.data, folder="Second")
        put_destination_copy(env, env.destination_id, unit, unit.data)  # forgets the other row
        drop_library_copy(env, unit)
        host = HostContentStore(env.engine, capabilities=registry)

        # the only file the registry knows is in the tree of the `readback: none` server
        with pytest.raises(ContentNeedsDownloadError):
            host.read(env.destination_id, ref(unit.item_id))


@pytest.mark.req("EF-EXP-046", "EF-EXP-001")
@pytest.mark.xfail(strict=True, reason="#305 m2: a rename gives the file another extension")
def test_a_rename_cannot_give_the_file_of_an_item_another_format() -> None:
    """The guard of the native format reads the `rel_path` of a `write` only. A plan that writes
    `Volume 1.cbz` (the format served) then renames it `Volume 1.pdf` puts the bytes of a CBZ under
    the name of a PDF: what `PlanBinding.native_formats` is there to refuse."""
    with export_env() as env:
        registry = Registry(env, caps("fetch"))
        fan, plans, _probe = _host_pass(env, registry)
        series = env.new_series()
        unit = unit_with_item(env, series)
        plans.by_destination = {
            env.destination_id: {
                "ops": [
                    {"op": "mkdir", "rel_path": "One"},
                    {
                        "op": "write",
                        "rel_path": "One/Volume 1.cbz",
                        "content_ref": {"kind": "export_item", "ref": unit.item_id},
                    },
                    {
                        "op": "rename",
                        "rel_path": "One/Volume 1.cbz",
                        "dst_rel_path": "One/Volume 1.pdf",
                    },
                ]
            }
        }

        fan.run(env.shelf_id, "call-1")

        assert not (env.lib.manga / "Kavita" / "One" / "Volume 1.pdf").exists()
