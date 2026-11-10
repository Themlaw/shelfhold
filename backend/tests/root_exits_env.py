"""Roots that are not right, on a real export tree (L25, EF-EXP-064): helpers for the tests."""

import json
import os
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.root_exits import RootExits, RootWatcher
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import platform
from shelfhold.infrastructure.secrets.tombstone_aead import kid_tag
from shelfhold.library.public import RootEvidence, SentinelEvidence
from tests.restore_env import RestoreEnv

SENTINEL = ".shelfhold-mount-id"
FOREIGN_INSTANCE = "01J0000000000000000FOREIGN"


class _Evidence:
    def __init__(self, env: RestoreEnv, fsops: Fsops) -> None:
        self._evidence = SentinelEvidence(env.keyring(), fsops)
        self._fsops = fsops

    def examine(self, root_path: str, candidates: Sequence[str]) -> dict[str, RootEvidence]:
        return self._evidence.examine(root_path, candidates)

    def tombstone_kids(self, root_path: str) -> dict[str, dict[str, str]]:
        return self._fsops.tombstone_kids(root_path)


def exits(
    env: RestoreEnv, *, keyring: bool = False, store_path: str | None = None
) -> tuple[RootExits, RootWatcher]:
    watcher = RootWatcher(env.engine, umask=0o022, store_path=store_path)
    return (
        RootExits(
            env.engine, clock=env.ex.clock, umask=0o022, store_path=store_path,
            locks=ResourceLocks(env.engine, clock=env.ex.clock), watcher=watcher,
            evidence=(lambda fsops: _Evidence(env, fsops)) if keyring else (lambda _f: None),
        ),
        watcher,
    )  # fmt: skip


def authentic_kids(env: RestoreEnv, rid: str, *kids: str) -> list[dict[str, object]]:
    """Registry entries whose tags the keyring of `env` verifies, bound to `rid`."""
    key = env.keyring().active
    return [{"kid": k, "tags": {key.kid: kid_tag(key.material, rid, k)}} for k in kids]


def root_id(env: RestoreEnv) -> str:
    with env.engine.connect() as connection:
        row = connection.execute(
            sa.select(platform.physical_root.c.root_id).where(
                platform.physical_root.c.path_entry == str(env.library)
            )
        )
        return str(row.scalar_one())


def write_sentinel(root: Path, **fields: object) -> None:
    path = root / SENTINEL
    document = json.loads(path.read_text("utf-8")) if path.exists() else {}
    document.update(fields)
    path.write_text(json.dumps(document), "utf-8")


def tree(root: Path) -> dict[str, bytes]:
    """Every file under `root` (the leases excluded: they come and go by themselves)."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != "lease" and not p.is_symlink()
    }


def assert_nothing_lost(before: dict[str, bytes], root: Path) -> None:
    after = tree(root)
    lost = sorted(k for k in before if k not in after and k != SENTINEL)
    assert not lost, f"files disappeared: {lost}"
    changed = sorted(k for k in before if k in after and before[k] != after[k] and k != SENTINEL)
    assert not changed, f"files changed: {changed}"
    assert os.path.exists(root / SENTINEL)
