"""M4 L53: kill -9 in the middle of `library normalize-pages`. The received file is copied to /store
before the library is rewritten, the rewrite is one atomic rename, and the bookkeeping follows. A
kill between any two of them leaves the original readable somewhere; the command run again finishes
and the result is the one of a run that was never interrupted."""

from __future__ import annotations

import hashlib
import signal
import subprocess
import sys
import textwrap

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal
from shelfhold.infrastructure.schema import storage
from shelfhold.library.public import independent_copies
from tests import page_formats_env as pf
from tests.integration.test_page_format_command import file_unit, normalizer
from tests.library_env import lib_env

pytestmark = pytest.mark.skipif(not pf.has_avif(), reason="this Pillow has no AVIF")

STAGES = ("after_keep", "after_rewrite")

CHILD = textwrap.dedent(
    """
    import os, signal, sys
    from pathlib import Path
    from shelfhold.export.domain.path_map import PathEntry, PathMap
    from shelfhold.export.domain.physical_roots import ExpectedRoot
    from shelfhold.infrastructure.db import create_db_engine
    from shelfhold.infrastructure.fsops import Fsops
    from shelfhold.library.public import KeepCopier
    from tests import page_formats_env as pf

    db, library, store, data, lib_root, store_root, instance, stage = sys.argv[1:9]
    engine = create_db_engine(Path(db))
    fsops = Fsops.open(
        PathMap((PathEntry(f"{library}/manga", "/manga"), PathEntry(store, "/store"))),
        [ExpectedRoot(lib_root, library, instance, 1),
         ExpectedRoot(store_root, store, instance, 1)],
    )
    fsops.boot()

    def locate(local):
        for root_id, base in ((store_root, store), (lib_root, library)):
            try:
                return root_id, Path(local).relative_to(base).as_posix()
            except ValueError:
                continue
        raise ValueError(local)

    def die(owner, name):
        real = getattr(owner, name)

        def wrapper(*args, **kwargs):
            result = real(*args, **kwargs)
            os.kill(os.getpid(), signal.SIGKILL)
            return result

        setattr(owner, name, wrapper)

    if stage == "after_keep":
        die(KeepCopier, "_keep")
    else:
        die(fsops, "write_atomic")
    pf.build_normalizer(
        engine, fsops, locate, store=store, data=Path(data), workdir=Path(data)
    ).run()
    """
)


@pytest.mark.req("EF-EXP-046", "EF-EXP-041")
@pytest.mark.parametrize("stage", STAGES)
def test_kill9_during_normalize_pages_then_the_command_finishes(stage: str) -> None:
    original = pf.mixed_cbz()
    with lib_env() as env:
        unit, dest = file_unit(env, original, "S - 1.cbz", first=True)
        db = str(env.engine.url.database)
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", CHILD, db, str(env.library), str(env.store), str(env.data),
             env.library_root, env.store_root, env.instance_id, stage],
            check=False, capture_output=True,
        )  # fmt: skip
        assert result.returncode == -signal.SIGKILL, result.stderr.decode()
        env.fsops.boot()
        IntentJournal(env.engine, env.fsops).resolve_open()
        summary = normalizer(env).run()
        assert summary.failed == 0
        (kept,) = env.files_under(env.store)
        assert kept.read_bytes() == original  # the original is whole, whatever the stage
        served = dest.read_bytes()
        assert pf.names(served)[0] == "001.jpg"
        with env.engine.connect() as c:
            row = c.execute(sa.select(storage.content)).one()
            assert row.derived_hash == "sha256:" + hashlib.sha256(served).hexdigest()
            assert independent_copies(c, unit) == 1  # only /store holds the received bytes
        assert normalizer(env).run().normalized == 0  # a second run moves nothing
        assert dest.read_bytes() == served
