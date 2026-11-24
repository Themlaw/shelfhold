"""The lifecycle of an installed third-party plugin (EF-SRC-020, D15, M6 L09a).

Update is the admin's manual act, from the index the install is pinned to (L08): the wheel the
index vouches for, fetched and verified like an install, then through the guard. A **pinned**
plugin never moves — not by a refresh (which believes the index, never the installed files), not
by ``update``. The wheel of the installed version is kept, so an update can put the previous
version aside, and the **rollback** — a reserved action, a fresh admin session or the host CLI —
returns to that previous kept version, the one and only one; after it, there is no previous
anymore. Uninstalling (``remove``) takes the code away and marks the bindings of the plugin's
sources orphaned, without losing anything they hold; a reinstall of the same key at the same
signer reattaches them.

Every refusal is explicit and leaves nothing behind: no file of a refused wheel stays on disk,
and the recorded state (the pin, the previous version) is untouched. The audit line is the
caller's, handed over as a callback and written in the same transaction as the change it
vouches for: no line without the act, no act without its line.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.catalog.application.orphan_bindings import orphan_bindings, reattach_orphaned
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import acquisition
from shelfhold.plugin_host import install as guard
from shelfhold.plugin_host.install import (
    Installed,
    Layout,
    drop_kept_wheels,
    file_sha256,
    install_wheel,
    keep_wheel,
    kept_wheel_path,
    previous_wheel_path,
    remove_plugin,
    wheel_plugin_keys,
)

if TYPE_CHECKING:  # the index package's __init__ imports its service, which imports this module:
    # the entry is a type of a signature, never a value here (no cycle at import time)
    from shelfhold.plugin_host.index.document import IndexEntry

__all__ = [
    "InstallRow",
    "PluginLifecycleError",
    "pin",
    "read_install",
    "reattach_if_same_origin",
    "retire_install",
    "rollback",
    "unpin",
    "update",
]


class PluginLifecycleError(Exception):
    """A refused lifecycle act, with its closed code and the detail the admin reads."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class _Store(Protocol):
    """The client of a pinned index (L07/L08): the refresh, the revocations, the wheel."""

    def list(self) -> list[Any]: ...

    async def refresh(self, record: Any, fetcher: Any, *, principal: Any = None) -> Any: ...

    def revoked_plugin_keys(self) -> frozenset[str]: ...

    def revoked_wheel_sha256(self) -> frozenset[str]: ...

    async def fetch_wheel(
        self, entry: IndexEntry, fetcher: Any, destination: Path, *, name: str | None = None
    ) -> Path: ...


@dataclass(frozen=True)
class InstallRow:
    """The lifecycle columns of ``plugin_install``: what the row keeps for this module."""

    plugin_id: str
    version: str
    sha256: str
    index_url: str | None
    signer_pubkey: str | None
    pinned: bool
    previous_version: str | None
    previous_sha256: str | None


def read_install(engine: Engine, key: str) -> InstallRow | None:
    install = acquisition.plugin_install
    with engine.connect() as connection:
        row = connection.execute(
            sa.select(
                install.c.version,
                install.c.sha256,
                install.c.index_url,
                install.c.signer_pubkey,
                install.c.pinned,
                install.c.previous_version,
                install.c.previous_sha256,
            ).where(install.c.plugin_id == key)
        ).first()
    return None if row is None else InstallRow(
        key, row.version, row.sha256, row.index_url, row.signer_pubkey,
        bool(row.pinned), row.previous_version, row.previous_sha256,
    )  # fmt: skip


def _write(
    engine: Engine,
    key: str,
    audit: Callable[[Any], None] | None = None,
    **values: Any,
) -> None:
    """Change the lifecycle columns of a row that exists; the pin (index, signer) is L08's and
    never moves here. The caller's audit callback runs in the same transaction: no lifecycle
    change without its line, no line without the change."""
    with write_transaction(engine) as connection:
        changed = connection.execute(
            sa.update(acquisition.plugin_install)
            .where(acquisition.plugin_install.c.plugin_id == key)
            .values(**values)
        ).rowcount
        if not changed:
            raise PluginLifecycleError("unknown", f"{key!r} is not an installed plugin")
        if audit is not None:
            audit(connection)


def _installed(layout: Layout, key: str, dist: str) -> guard.Installed:
    plugin = guard.read_state(layout).installed.get(dist)
    if plugin is None:
        raise PluginLifecycleError("unknown", f"{key!r} is not an installed plugin")
    return plugin


def _prefixed(value: str) -> str:
    return value if value.startswith("sha256:") else f"sha256:{value}"


def pin(
    engine: Engine,
    layout: Layout,
    key: str,
    dist: str,
    audit: Callable[[Any], None] | None = None,
) -> InstallRow:
    """Freeze the installed version: no update, no rollback, until the admin unpins. The pin is
    the admin's word for *this* code, and it outlives the index saying otherwise."""
    _installed(layout, key, dist)
    row = read_install(engine, key)
    if row is None:
        raise PluginLifecycleError("unknown", f"{key!r} is not an installed plugin")
    if not row.pinned:
        _write(engine, key, audit, pinned=True)
    return read_install(engine, key) or row


def unpin(
    engine: Engine,
    layout: Layout,
    key: str,
    dist: str,
    audit: Callable[[Any], None] | None = None,
) -> InstallRow:
    _installed(layout, key, dist)
    row = read_install(engine, key)
    if row is None:
        raise PluginLifecycleError("unknown", f"{key!r} is not an installed plugin")
    if row.pinned:
        _write(engine, key, audit, pinned=False)
    return read_install(engine, key) or row


def _version_key(version: str) -> list[tuple[int, str]]:
    """A dot-separated version compared numerically where both parts are numbers, textually
    otherwise — enough to tell an update from a rollback, without a dependency."""
    return [
        (int(part), "") if part.isdigit() else (-1, part)
        for part in version.replace("-", ".").split(".")
    ]


def _kept_wheel_of(layout: Layout, key: str, wheel_sha256: str) -> Path:
    """The kept wheel must be the one the recorded install came from, byte for byte (the guard's
    own record of the wheel it installed): an update that cannot prove what it puts aside is
    refused — there would be no rollback afterwards, only a promise."""
    kept = kept_wheel_path(layout, key)
    if not kept.exists() or file_sha256(kept) != wheel_sha256.removeprefix("sha256:"):
        raise PluginLifecycleError(
            "previous_unavailable",
            "the kept wheel of the installed version is missing or no longer the recorded one",
        )
    return kept


async def update(
    engine: Engine,
    store: _Store,
    layout: Layout,
    key: str,
    dist: str,
    fetcher: Any,
    audit: Callable[[Any], None] | None = None,
) -> Installed:
    """Install the version the pinned index vouches for now, keeping the previous one for the
    rollback. Refused: a pinned plugin, a plugin no index vouches for, a revoked key or wheel,
    the same version (nothing to do), a lower one (that is a rollback, a reserved act), or a kept
    wheel that cannot prove what it is. The new wheel is verified before the old files move:
    a refusal leaves the installed plugin exactly as it was."""
    plugin = _installed(layout, key, dist)
    row = read_install(engine, key)
    if row is None:
        raise PluginLifecycleError("unknown", f"{key!r} is not an installed plugin")
    if row.pinned:
        raise PluginLifecycleError(
            "pinned", "the installed version is pinned: unpin it to update the plugin"
        )
    if row.index_url is None:
        raise PluginLifecycleError(
            "not_from_index", "a wheel the admin gave by hand has no index to update from"
        )
    record = next((r for r in store.list() if r.url == row.index_url), None)
    if record is None:
        raise PluginLifecycleError(
            "index_not_pinned", "the repository this plugin was installed from is no longer pinned"
        )
    outcome = await store.refresh(record, fetcher)
    entry = next((e for e in outcome.document.plugins if e.key == key), None)
    if entry is None:
        raise PluginLifecycleError("unknown_plugin", f"the index does not list {key!r}")
    if key in store.revoked_plugin_keys():
        raise PluginLifecycleError("plugin_revoked", f"the key of {key!r} was revoked")
    if entry.sha256 in store.revoked_wheel_sha256():
        raise PluginLifecycleError("wheel_revoked", "the index revoked this wheel")
    if entry.version == row.version:
        raise PluginLifecycleError(
            "already_newest", "the installed version is the one the index lists"
        )
    if _version_key(entry.version) < _version_key(row.version):
        raise PluginLifecycleError(
            "rollback",
            "the index lists a lower version than the installed one: that is a rollback "
            "(a reserved action), not an update",
        )
    from shelfhold.plugin_host.index.staging import discard_staged_wheel

    wheel_name = PurePosixPath(urlsplit(entry.wheel_url).path).name
    if not wheel_name.endswith(".whl") or wheel_name == ".whl" or not wheel_name[:-4]:
        raise PluginLifecycleError(
            "wheel_name_invalid", "the wheel address does not name a wheel file"
        )
    kept = _kept_wheel_of(layout, key, plugin.wheel_sha256)
    wheel = await store.fetch_wheel(entry, fetcher, layout.root / "index", name=wheel_name)
    try:
        if wheel_plugin_keys(wheel) != frozenset({key}):
            raise PluginLifecycleError(
                "wheel_key_mismatch",
                "the wheel does not declare exactly the key its index entry names",
            )
        remove_plugin(layout, dist)
        try:
            installed = install_wheel(layout, wheel, entry.sha256)
        except guard.PluginInstallError:
            with contextlib.suppress(guard.PluginInstallError):
                # the old version back, nothing lost; if even that fails, the refusal says what
                # happened and the kept wheel is still there to reinstall from
                install_wheel(layout, kept, row.sha256)
            raise
        # the update succeeded: the old version's wheel becomes the kept previous
        aside = previous_wheel_path(layout, key)
        aside.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        kept.replace(aside)
        keep_wheel(layout, key, wheel)
    finally:
        discard_staged_wheel(wheel)  # the installed copy is the guard's own
    _write(
        engine,
        key,
        audit,
        version=installed.version,
        sha256=_prefixed(installed.digest),  # the files digest, like the attestation sync
        previous_version=row.version,
        previous_sha256=_prefixed(plugin.wheel_sha256),  # proven byte for byte above
    )
    return installed


def rollback(
    engine: Engine,
    layout: Layout,
    key: str,
    dist: str,
    audit: Callable[[Any], None] | None = None,
) -> Installed:
    """Return to the previous kept version — the one the last update put aside, and the only
    one. A reserved action (the caller checked); refused when the plugin is pinned or when no
    previous version was kept. After it there is no previous anymore: the version that was
    installed is not kept, going back and forth is not what the reserved act is for."""
    _installed(layout, key, dist)
    row = read_install(engine, key)
    if row is None:
        raise PluginLifecycleError("unknown", f"{key!r} is not an installed plugin")
    if row.pinned:
        raise PluginLifecycleError(
            "pinned", "the installed version is pinned: unpin it to roll the plugin back"
        )
    if row.previous_version is None or row.previous_sha256 is None:
        raise PluginLifecycleError(
            "no_previous", "no previous kept version to return to (none was kept aside)"
        )
    previous = previous_wheel_path(layout, key)
    if not previous.exists() or file_sha256(previous) != row.previous_sha256.removeprefix(
        "sha256:"
    ):
        raise PluginLifecycleError(
            "previous_unavailable",
            "the kept wheel of the previous version is missing or no longer the recorded one",
        )
    from shelfhold.plugin_host.index.staging import discard_staged_wheel, stage_wheel

    # the installer reads the tags of the *filename*: the kept wheel is staged under the name
    # its own distribution and version answer for, and the staged copy leaves with the install
    handle, staged = stage_wheel(
        layout.root / "index",
        f"{dist.replace('-', '_')}-{row.previous_version}-py3-none-any.whl",
    )
    try:
        with os.fdopen(handle, "wb") as sink:
            sink.write(previous.read_bytes())
        remove_plugin(layout, dist)
        installed = install_wheel(layout, staged, row.previous_sha256)
    finally:
        discard_staged_wheel(staged)
    previous.replace(kept_wheel_path(layout, key))  # the rolled-back version is the kept one now
    _write(
        engine,
        key,
        audit,
        version=installed.version,
        sha256=_prefixed(installed.digest),  # the files digest, like the attestation sync
        previous_version=None,
        previous_sha256=None,
    )
    return installed


def retire_install(
    engine: Engine,
    layout: Layout,
    key: str,
    audit: Callable[[Any], None] | None = None,
) -> int:
    """The uninstall's own bookkeeping, once the guard has removed the files: the bindings of
    the plugin's sources are marked orphaned (nothing they hold is lost), the lifecycle columns
    go — no version, no pin, no previous — and the kept wheels follow the code. The pin of L08
    (index, signer) stays: the row says whose word installed the plugin, and a reinstall of the
    same key at the same signer reattaches what waited."""
    with write_transaction(engine) as connection:
        marked = orphan_bindings(connection, key)
        connection.execute(
            sa.update(acquisition.plugin_install)
            .where(acquisition.plugin_install.c.plugin_id == key)
            .values(pinned=False, previous_version=None, previous_sha256=None)
        )
        if audit is not None:
            audit(connection)
    drop_kept_wheels(layout, key)
    return marked


def reattach_if_same_origin(
    engine: Engine, key: str, index_url: str | None, signer: str | None
) -> int:
    """After a reinstall: the orphaned bindings of the key come back when the origin is the same
    — the same index and the same signer, or the admin's own hand for a wheel given by hand.
    Another signer's code does not inherit the bindings (GAP-8-13: nothing is inherited without
    the admin's confirmed word)."""
    row = read_install(engine, key)
    if row is None or (row.index_url, row.signer_pubkey) != (index_url, signer):
        return 0
    with write_transaction(engine) as connection:
        return reattach_orphaned(connection, key)
