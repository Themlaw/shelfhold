"""#348 (M4 L47), contradicteur: the alias an export writes is sampled from a file nobody verified.

`record_export_alias` reads the KOReader and Kavita samples of the file at `path` with a plain
`open()`, inside the `BEGIN IMMEDIATE` of the manifest, and stores them under `file_hash` without
checking that the bytes read are the bytes `file_hash` names. `heal_alias_hashes` does the very
same job with the opposite contract (one pass, SHA-256 and samples from the same blocks, "a file
rewritten since never lends its hashes to another's alias", no symbolic link). In the fan-out the
file is read in `FanOut._keep`, after the whole plan ran: whatever is at the path then (a link to
another file, another file's bytes, a FIFO left in a shared destination folder) is what the alias
records.

- the alias of A carries the digest of B: B's digest becomes ambiguous and its positions are
  refused (the e-reader of B loses its sync);
- a FIFO at the path: the open blocks for ever, with the database write lock held.
"""

import hashlib
import io
import os
import threading

import pytest
import sqlalchemy as sa

from shelfhold.export.application.export_alias import record_export_alias
from shelfhold.infrastructure.schema import export
from shelfhold.reading.domain.document_hash import koreader_partial_md5
from tests.bundle_env import Chapter
from tests.export_env import export_env
from tests.integration.test_l47_kosync import (
    lab,  # noqa: F401  (fixture)
    library,
    positions,
    pull,
    push,
)
from tests.integration.test_l48_opds import World
from tests.upgrade_env import export_one

pytestmark = pytest.mark.req("EF-LEC-009", "ENF-QUAL-005")


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.mark.xfail(
    strict=True,
    reason="#348 contradicteur: the alias samples the file at its path without checking it is "
    "the file `file_hash` names (and follows a link): A's alias takes B's digest, B is ambiguous",
)
def test_the_alias_never_takes_the_digest_of_another_file(lab: World) -> None:  # noqa: F811
    a = library(lab, "ok").add(Chapter.simple("1", count=20))
    b = library(lab, "ok").add(Chapter.simple("2", count=20))
    a_bytes, b_bytes = a.path.read_bytes(), b.path.read_bytes()
    assert a_bytes != b_bytes
    with lab.seed.engine.begin() as connection:  # B exported first, its alias is right
        assert record_export_alias(
            connection, destination_id=None, export_item_id=b.item_id, file_hash=sha(b_bytes),
            path=b.path,
        )  # fmt: skip
    # A was written as `a_bytes`; before `FanOut._keep` reads it, its path is a link to B
    a.path.unlink()
    a.path.symlink_to(b.path)
    with lab.seed.engine.begin() as connection:
        record_export_alias(
            connection, destination_id=None, export_item_id=a.item_id, file_hash=sha(a_bytes),
            path=a.path,
        )  # fmt: skip
    digest_b = koreader_partial_md5(io.BytesIO(b_bytes))
    with lab.seed.engine.connect() as connection:
        holders = set(
            connection.execute(
                sa.select(export.document_alias.c.export_item_id).where(
                    export.document_alias.c.koreader_partial_md5 == "md5:" + digest_b
                )
            ).scalars()
        )
    assert holders == {b.item_id}  # only B's file is named by B's digest
    assert push(lab.mum, digest_b, "4", 4 / 20).status_code == 200
    assert pull(lab.mum, digest_b).json().get("progress") == "4"
    assert len(positions(lab)) == 1


@pytest.mark.xfail(
    strict=True,
    reason="#348 contradicteur: the alias opens the destination file with a plain open() inside "
    "the write transaction: a FIFO at the path blocks it, the database write lock held",
)
def test_a_fifo_at_the_path_never_blocks_the_alias() -> None:
    with export_env() as env:
        exported = export_one(env)
        with env.engine.begin() as connection:
            connection.execute(sa.delete(export.document_alias))
        exported.target.unlink()
        os.mkfifo(exported.target)
        failures: list[BaseException] = []

        def close_the_line() -> None:
            try:
                with env.engine.begin() as connection:
                    record_export_alias(
                        connection, destination_id=env.destination_id,
                        export_item_id=exported.export_item_id,
                        file_hash=exported.old_file_hash, path=exported.target,
                    )  # fmt: skip
            except BaseException as error:  # reported below
                failures.append(error)

        worker = threading.Thread(target=close_the_line, daemon=True)
        worker.start()
        worker.join(3)
        blocked = worker.is_alive()
        if blocked:  # let it go: a writer opens and closes the FIFO, the reader sees its end
            os.close(os.open(exported.target, os.O_WRONLY | os.O_NONBLOCK))
            worker.join(10)
        assert not blocked, "record_export_alias blocked on a FIFO inside the transaction"
        assert failures == []
