"""EF-INT-026, EF-EXP-029, ENF-OPS-015 (ADR-0044): an `Fsops` that is a read view writes nothing.

`Composition.read_context()` hands out such a view: a deletion or a write that goes by that door
would pass the lease and the look at the whole instance of a write session. Structural guard:
every public method of `Fsops` is classified here as a read or a write; each write is tried on
a view and must raise `ReadOnlyFsopsError` before it touches anything, whatever its arguments.
A method added to `Fsops` and not classified fails the first test.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from shelfhold.infrastructure.fsops import Fsops, ReadOnlyFsopsError
from tests.fsops_env import Env, make_env

pytestmark = pytest.mark.req("EF-INT-026", "EF-EXP-029")

READS = frozenset(
    [
        "open",
        "frozen",
        "freeze_state",
        "writes_refused",
        "refuse_writes",
        "on_root_blocked",
        "on_restored_behind",
        "freeze",
        "unfreeze",
        "on_disk_pressure",
        "enforce_lease",
        "enforce_tombstone_flush",
        "status",
        "root_state",
        "require_scan_allowed",
        "require_delete_allowed",
        "require_stampable",
        "sentinel_of",
        "root_frozen",
        "read_bytes",
        "open_read",
        "free_bytes",
        "device_of",
        "link_stat",
        "observe",
        "fingerprint",
        "list_dir",
        "is_read_only",
        "sha256_of",
        "hash_and_identity",
        "read_lease",
        "read_mirror",
        "list_mirror",
        "tombstone_chain",
        "tombstone_chain_written",
        "read_tombstone_journal",
        "tombstone_kids",
        "trash_expired",
        "early_purge_allowed",
        "same_path_bin_entry",
        "same_path_state",
        "open_bin_entry",
    ]
)

WRITES = frozenset(
    [
        "update_lease",
        "write_mirror",
        "append_tombstone_line",
        "replace_tombstone_journal",
        "record_tombstone_chain",
        "record_tombstone_kid",
        "set_aside_tombstone_journal",
        "write_atomic",
        "probe_capabilities",
        "mkdir",
        "utime",
        "unlink",
        "purge_unlink",
        "purge_remove_tree",
        "erase_host_area",
        "system_unlink",
        "system_remove_tree",
        "rmdir",
        "rename",
        "relocate_inbox",
        "recycle",
        "restore_from_bin",
        "same_path_stage",
        "same_path_swap",
        "same_path_recycle_old",
        "same_path_discard",
        "copy_across",
        "copy_independent",
        "link_replaceable",
        "boot",
        "stamp_sentinel",
        "restamp_after_restore",
        "takeover_sentinel",
    ]
)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path, generation=3)


def public_methods() -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(Fsops)
        if not name.startswith("_") and (callable(member) or isinstance(member, property))
    }


def test_every_public_method_of_fsops_is_classified_a_read_or_a_write() -> None:
    assert READS.isdisjoint(WRITES)
    assert public_methods() == READS | WRITES, "classify the new method of Fsops"


def test_every_write_method_carries_the_guard() -> None:
    unguarded = [n for n in sorted(WRITES) if not getattr(getattr(Fsops, n), "__writes__", False)]
    assert unguarded == []


def dummy(parameter: inspect.Parameter) -> Any:
    annotation = str(parameter.annotation)
    if "bytes" in annotation:
        return b"x"
    if "int" in annotation:
        return 1
    if "Callable" in annotation:
        return lambda *a, **k: None
    if "bool" in annotation:
        return False
    return "/x"


@pytest.mark.parametrize("name", sorted(WRITES))
def test_a_read_view_refuses_every_write_before_it_touches_anything(env: Env, name: str) -> None:
    view = env.fsops
    view.refuse_writes()
    method = getattr(view, name)
    arguments = [
        dummy(p)
        for p in inspect.signature(method).parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    keywords = {
        p.name: dummy(p)
        for p in inspect.signature(method).parameters.values()
        if p.default is inspect.Parameter.empty and p.kind is p.KEYWORD_ONLY
    }
    before = sorted(str(p) for p in env.library.rglob("*"))
    with pytest.raises(ReadOnlyFsopsError):
        result = method(*arguments, **keywords)
        if hasattr(result, "__enter__"):
            with result:
                pass
    assert sorted(str(p) for p in env.library.rglob("*")) == before


def test_the_reads_still_work_on_a_read_view(env: Env) -> None:
    env.fsops.refuse_writes()
    assert env.fsops.writes_refused
    assert env.fsops.root_state(str(env.library)) is not None
    assert env.fsops.sentinel_of(str(env.library)) is not None
