"""M4 L53: `shelfhold library normalize-pages [--dry-run]` brings an existing library to the
allowlisted formats. The received file goes to /store first, then /library takes the derivative;
a second run finds nothing to do and changes no byte."""

from __future__ import annotations

import argparse
import hashlib
from contextlib import nullcontext
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.schema import platform, storage
from shelfhold.interfaces.cli.registry import build_parser
from shelfhold.library.public import (
    PageStoreNotConfiguredError,
    PassthroughStore,
    independent_copies,
)
from tests import page_formats_env as pf
from tests.library_env import LibEnv, lib_env
from tests.null_tombstones import NONE_PURGED

pytestmark = pytest.mark.skipif(not pf.has_avif(), reason="this Pillow has no AVIF")


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def file_unit(env: LibEnv, data: bytes, name: str, *, first: bool = False) -> tuple[str, Path]:
    """A Unit filed by the plain store, as a library from before the normalization has it."""
    with env.engine.begin() as connection:
        if not connection.execute(sa.select(platform.instance)).first():
            connection.execute(
                sa.insert(platform.instance).values(instance_id=env.instance_id, jurisdiction="FR")
            )
    unit, variant = (env.chain["unit"], env.chain["variant"]) if first else env.new_unit()
    plain = PassthroughStore(
        env.engine, env.fsops, env.locate, env.data,
        tombstones=NONE_PURGED, hold_work=lambda _work: nullcontext(),
    )  # fmt: skip
    dest = env.manga / "S" / name
    plain.ingest(
        unit_id=unit, variant_id=variant, staged=env.stage(data), dest=str(dest),
        allow_convertible_pages=True,
    )  # fmt: skip
    return unit, dest


def normalizer(env: LibEnv, *, store_configured: bool = True):
    return pf.build_normalizer(
        env.engine, env.fsops, env.locate, store=str(env.store), data=env.data, workdir=env.tmp,
        store_configured=store_configured,
    )  # fmt: skip


@pytest.mark.req("EF-EXP-046")
def test_the_command_exists_and_takes_dry_run() -> None:
    args = build_parser().parse_args(["library", "normalize-pages", "--dry-run"])
    assert isinstance(args, argparse.Namespace) and args.dry_run is True


@pytest.mark.req("EF-EXP-046")
def test_dry_run_lists_what_would_change_and_changes_nothing() -> None:
    original = pf.mixed_cbz()
    with lib_env() as env:
        unit, dest = file_unit(env, original, "S - 1.cbz", first=True)
        file_unit(env, pf.cbz([("1.jpg", pf.jpeg())]), "S - 2.cbz")
        summary = normalizer(env).run(dry_run=True)
        assert summary.dry_run and [c.unit_id for c in summary.pending] == [unit]
        assert dest.read_bytes() == original and env.files_under(env.store) == []


@pytest.mark.req("EF-EXP-046", "EF-BIB-027")
def test_apply_keeps_the_original_and_normalizes_the_library_then_is_idempotent() -> None:
    original = pf.mixed_cbz()
    with lib_env() as env:
        unit, dest = file_unit(env, original, "S - 1.cbz", first=True)
        run = normalizer(env)
        summary = run.run()
        assert summary.normalized == 1 and summary.failed == 0
        (kept,) = env.files_under(env.store)
        assert kept.read_bytes() == original
        served = dest.read_bytes()
        with env.engine.connect() as c:
            row = c.execute(sa.select(storage.content)).one()
            assert (
                row.derived_hash == sha(served) and independent_copies(c, unit) == 1
            )  # only /store holds the received bytes
        again = normalizer(env).run()
        assert again.normalized == 0 and not again.pending
        assert dest.read_bytes() == served  # not a byte moved


@pytest.mark.req("EF-EXP-046")
def test_a_unit_with_an_unreadable_page_is_reported_and_left_the_others_go_on() -> None:
    bad = pf.cbz([("1.avif", pf.avif()), ("2.avif", pf.truncated_avif())])
    with lib_env() as env:
        _, bad_dest = file_unit(env, bad, "S - 1.cbz", first=True)
        _, good_dest = file_unit(env, pf.mixed_cbz(), "S - 2.cbz")
        summary = normalizer(env).run()
        assert summary.normalized == 1 and summary.kept_as_received == 1
        assert bad_dest.read_bytes() == bad
        assert [(i.entry, i.code) for r in summary.reports for i in r.issues] == [
            ("2.avif", "page.undecodable")
        ]
        assert pf.names(good_dest.read_bytes())[0] == "001.jpg"


@pytest.mark.req("EF-EXP-046")
def test_it_refuses_to_rewrite_the_library_without_a_store() -> None:
    with lib_env() as env:
        file_unit(env, pf.mixed_cbz(), "S - 1.cbz", first=True)
        with pytest.raises(PageStoreNotConfiguredError) as refused:
            normalizer(env, store_configured=False).run()
        assert refused.value.code == "pages.store_not_configured"
