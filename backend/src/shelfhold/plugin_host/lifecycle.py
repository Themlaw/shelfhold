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
from shelfhold.infrastructure.schema import acquisition, platform
from shelfhold.plugin_host import install as guard
from shelfhold.plugin_host import state as plugin_state
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

from shelfhold import __version__ as HOST_VERSION

__all__ = [
    "HOST_VERSION",
    "InstallRow",
    "PluginLifecycleError",
    "announce_incompatible",
    "incompatible_installed",
    "pin",
    "read_install",
    "reattach_if_same_origin",
    "retire_install",
    "rollback",
    "unpin",
    "update",
    "version_key",
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


def version_key(version: str) -> list[tuple[int, str]]:
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


def _staged_name(dist: str, version: str, wheel: Path) -> str:
    """The name a kept wheel is staged under: its own distribution, its own version, and the
    tags its own metadata declares — the installer reads all three from the filename, and a
    kept copy is stored under the key's name, which no installer accepts."""
    return f"{dist.replace('-', '_')}-{version}-{guard.inspect_wheel(wheel).tags}.whl"


def _stage_kept(layout: Layout, wheel: Path, name: str) -> Path:
    """Stage a kept wheel under a name the installer accepts; the staged copy leaves with the
    install."""
    from shelfhold.plugin_host.index.staging import discard_staged_wheel, stage_wheel

    handle, staged = stage_wheel(layout.root / "index", name)
    try:
        with os.fdopen(handle, "wb") as sink:
            sink.write(wheel.read_bytes())
    except OSError as exc:
        discard_staged_wheel(staged)
        raise guard.PluginInstallError("bad_wheel", str(exc)) from exc
    return staged


def _reinstall_kept(layout: Layout, key: str, dist: str, version: str, sha256: str) -> None:
    """After a failed act, the version that was installed goes back from its kept wheel —
    nothing lost; if even that fails, the refusal says what happened and the kept wheels are
    still there. The sha is the guard's own record of the wheel (the row's is the files
    digest)."""
    from shelfhold.plugin_host.index.staging import discard_staged_wheel

    kept = kept_wheel_path(layout, key)
    if not kept.exists():
        raise guard.PluginInstallError(
            "bad_wheel", "the kept wheel of the installed version is missing"
        )
    staged = _stage_kept(layout, kept, _staged_name(dist, version, kept))
    try:
        install_wheel(layout, staged, sha256)
    finally:
        discard_staged_wheel(staged)


def _next(path: Path) -> Path:
    """The staged name of a kept wheel, beside it: the rename onto it is the only step after
    the committed line."""
    return path.with_name(f"{path.name}.next")


def _stage_copies(layout: Layout, key: str, copies: tuple[tuple[Path, Path], ...]) -> None:
    try:
        for source, staged in copies:
            keep_wheel(layout, key, source, dest=staged)
    except OSError as exc:
        raise PluginLifecycleError(
            "kept_unwritable", f"the kept wheels cannot be written: {exc}"
        ) from exc


def _settle(renames: tuple[tuple[Path, Path], ...]) -> None:
    for staged, final in renames:
        with contextlib.suppress(OSError):
            final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            staged.replace(final)


def _drop(*paths: Path) -> None:
    for path in paths:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def _grant_of(layout: Layout, dist: str) -> tuple[Any, Any]:
    """The plugin's activation (the in-process confirmation, the isolated grant), read before
    the files move: a restored version is the same code, its activation comes back with it."""
    state = guard.read_state(layout)
    return state.confirmed.get(dist), state.enabled.get(dist)


def _restore(
    layout: Layout, key: str, dist: str, version: str, sha256: str, grant: tuple[Any, Any]
) -> None:
    """Undo a failed act: the version that was installed goes back from its kept wheel, with
    its activation when the files are the same ones byte for byte. If even the reinstall
    fails, the refusal says what happened and the kept wheel is still there."""
    with contextlib.suppress(guard.PluginInstallError):
        remove_plugin(layout, dist)
    try:
        _reinstall_kept(layout, key, dist, version, sha256)
    except (guard.PluginInstallError, OSError):
        return
    state = guard.read_state(layout)
    back = state.installed.get(dist)
    confirmed, enabled = grant
    if back is None:
        return
    if confirmed is not None and confirmed.digest == back.digest:
        state.confirmed[dist] = confirmed
    if enabled is not None and enabled.digest == back.digest:
        state.enabled[dist] = enabled
    guard.write_state(layout, state)


async def update(
    engine: Engine,
    store: _Store,
    layout: Layout,
    key: str,
    dist: str,
    fetcher: Any,
    audit: Callable[[Any], None] | None = None,
    notice: Callable[[Any], None] | None = None,
) -> Installed:
    """Install the version the pinned index vouches for now, keeping the previous one for the
    rollback. Refused: a pinned plugin, a plugin no index vouches for, an index pinned again
    to another key (the install stays pinned to its own signer, L08), a revoked key or wheel,
    the same version (nothing to do), a lower one (that is a rollback, a reserved act), a
    version whose ``min_host`` is above the host, or a kept wheel that cannot prove what it
    is. The new wheel is verified before the old files move: a refusal leaves the installed
    plugin exactly as it was. ``notice``, when given, is told of a deprecated entry (with its
    ``replaced_by``): the index's word is the admin's to read, never an act of the host."""
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
    if record.pubkey != row.signer_pubkey:
        # the same address pinned again to another key is another signer's index (L08): the
        # install stays pinned to the signer whose word installed it, L08's own refusal
        raise PluginLifecycleError(
            "signer_pinned",
            "the repository at this address is pinned to another key now: this install stays "
            "pinned to its own signer — remove the plugin and install it as a distinct one",
        )
    outcome = await store.refresh(record, fetcher)
    entry = next((e for e in outcome.document.plugins if e.key == key), None)
    if entry is None:
        raise PluginLifecycleError("unknown_plugin", f"the index does not list {key!r}")
    if key in store.revoked_plugin_keys():
        raise PluginLifecycleError("plugin_revoked", f"the key of {key!r} was revoked")
    if entry.sha256 in store.revoked_wheel_sha256():
        raise PluginLifecycleError("wheel_revoked", "the index revoked this wheel")
    if version_key(entry.min_host) > version_key(HOST_VERSION):
        raise PluginLifecycleError(
            "min_host",
            f"the index vouches for a plugin that needs host {entry.min_host} or newer "
            f"(this host is {HOST_VERSION})",
        )
    if entry.version == row.version:
        raise PluginLifecycleError(
            "already_newest", "the installed version is the one the index lists"
        )
    if version_key(entry.version) < version_key(row.version):
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
    aside = previous_wheel_path(layout, key)
    staged_kept, staged_aside = _next(kept), _next(aside)
    try:
        if wheel_plugin_keys(wheel) != frozenset({key}):
            raise PluginLifecycleError(
                "wheel_key_mismatch",
                "the wheel does not declare exactly the key its index entry names",
            )
        # both kept wheels are written under their staged names before anything moves: a full
        # disk is a refusal that leaves the plugin as it was, never a step after the line
        _stage_copies(layout, key, ((wheel, staged_kept), (kept, staged_aside)))
        grant = _grant_of(layout, dist)
        remove_plugin(layout, dist)
        try:
            installed = install_wheel(layout, wheel, entry.sha256)
            # the line and the row in one transaction, the files already the new version: if
            # the line cannot be written, the act is undone — no act without its line
            _write(
                engine,
                key,
                audit,
                version=installed.version,
                sha256=_prefixed(installed.digest),  # the files digest, like the attestation sync
                previous_version=row.version,
                previous_sha256=_prefixed(plugin.wheel_sha256),  # proven byte for byte above
                api=str(entry.api),
                min_host=entry.min_host,
                replaced_by=entry.replaced_by,  # the new entry's word, like the install's pin
            )
        except Exception:
            _restore(layout, key, dist, row.version, plugin.wheel_sha256, grant)
            raise
        # committed: only renames are left, and a failed one fails closed (the kept wheel no
        # longer matches the record, the next update or rollback refuses)
        _settle(((staged_aside, aside), (staged_kept, kept)))
    finally:
        _drop(staged_kept, staged_aside)
        with contextlib.suppress(OSError):  # after the committed line, never a false failure
            discard_staged_wheel(wheel)  # the installed copy is the guard's own
    if entry.deprecated and notice is not None:
        notice(entry)
    return installed


def rollback(
    engine: Engine,
    store: _Store,
    layout: Layout,
    key: str,
    dist: str,
    audit: Callable[[Any], None] | None = None,
) -> Installed:
    """Return to the previous kept version — the one the last update put aside, and the only
    one. A reserved action (the caller checked); refused when the plugin is pinned, when no
    previous version was kept, or when the index revoked that version's wheel or the plugin's
    key: a revocation is the index's word against the very code the rollback would put back.
    After it there is no previous anymore: the version that was installed is not kept, going
    back and forth is not what the reserved act is for. A rollback that fails puts the
    installed version back first: the refusal leaves the plugin exactly as it was."""
    plugin = _installed(layout, key, dist)
    row = read_install(engine, key)
    if row is None:
        raise PluginLifecycleError("unknown", f"{key!r} is not an installed plugin")
    if row.pinned:
        raise PluginLifecycleError(
            "pinned", "the installed version is pinned: unpin it to roll the plugin back"
        )
    if key in store.revoked_plugin_keys():
        raise PluginLifecycleError("plugin_revoked", f"the key of {key!r} was revoked")
    if row.previous_version is None or row.previous_sha256 is None:
        raise PluginLifecycleError(
            "no_previous", "no previous kept version to return to (none was kept aside)"
        )
    newest_state = plugin_state.newest_written_version(engine, key)
    if newest_state is not None and version_key(newest_state) > version_key(row.previous_version):
        # the state was written by a version newer than the one the rollback would put back:
        # restoring it is not the reserved act's to decide — the plugin keeps its newer state
        raise PluginLifecycleError(
            "state_newer",
            f"the plugin state was written by {newest_state}, newer than the previous kept "
            f"version {row.previous_version}: rolling back would read state it cannot parse",
        )
    if row.previous_sha256 in store.revoked_wheel_sha256():
        raise PluginLifecycleError(
            "wheel_revoked", "the index revoked the wheel of the previous kept version"
        )
    previous = previous_wheel_path(layout, key)
    if not previous.exists() or file_sha256(previous) != row.previous_sha256.removeprefix(
        "sha256:"
    ):
        raise PluginLifecycleError(
            "previous_unavailable",
            "the kept wheel of the previous version is missing or no longer the recorded one",
        )
    from shelfhold.plugin_host.index.staging import discard_staged_wheel

    # the installer reads the tags of the *filename*: the kept wheel is staged under the name
    # its own metadata answers for — the tags its publisher chose, never a made-up section —
    # and the staged copy leaves with the install
    staged = _stage_kept(layout, previous, _staged_name(dist, row.previous_version, previous))
    kept = kept_wheel_path(layout, key)
    staged_kept = _next(kept)
    try:
        # the rolled-back version's wheel becomes the kept one: written under its staged name
        # before anything moves, so nothing fallible is left once the line is committed
        _stage_copies(layout, key, ((previous, staged_kept),))
        grant = _grant_of(layout, dist)
        remove_plugin(layout, dist)
        try:
            installed = install_wheel(layout, staged, row.previous_sha256)
            _write(
                engine,
                key,
                audit,
                version=installed.version,
                sha256=_prefixed(installed.digest),  # the files digest, like the attestation sync
                previous_version=None,
                previous_sha256=None,
            )
        except Exception:
            # the version that was installed, back — nothing lost; both kept wheels are still
            # where they were
            _restore(layout, key, dist, row.version, plugin.wheel_sha256, grant)
            raise
        _settle(((staged_kept, kept),))
        with contextlib.suppress(OSError):
            previous.unlink()  # no previous anymore, as the committed row says
    finally:
        _drop(staged_kept)
        with contextlib.suppress(OSError):  # after the committed line, never a false failure
            discard_staged_wheel(staged)
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
    engine: Engine,
    key: str,
    previous: tuple[str | None, str | None] | None,
    index_url: str | None,
    signer: str | None,
) -> int:
    """After a reinstall: the orphaned bindings of the key come back when the origin is the
    same — `previous` is the (index, signer) the *previous* install was pinned to, read before
    the new install rewrote the row, never the row itself (which now tells the new install's
    own origin). Another signer's code, or another index's, does not inherit the bindings
    (GAP-8-13: nothing is inherited without the admin's confirmed word)."""
    if previous is None or previous != (index_url, signer):
        return 0
    with write_transaction(engine) as connection:
        return reattach_orphaned(connection, key)


def incompatible_installed(
    engine: Engine, *, host_version: str, api_version: int
) -> dict[str, str]:
    """The installed plugins a host upgrade left behind, each with its closed reason: the
    entry's `min_host` is above the host's own version, or its `api` is out of the N/N-1
    window (the same one `negotiate_api` judges a manifest by). The fields are the ones the
    index declared and the install recorded — a later host judges by the index's own word,
    never by what it remembers of the document."""
    judged: dict[str, str] = {}
    with engine.connect() as connection:
        if not sa.inspect(connection).has_table("plugin_install"):
            return judged  # a database not migrated yet has no install to judge
        rows = connection.execute(
            sa.select(
                acquisition.plugin_install.c.plugin_id,
                acquisition.plugin_install.c.api,
                acquisition.plugin_install.c.min_host,
            )
        ).all()
    for row in rows:
        if row.min_host and version_key(row.min_host) > version_key(host_version):
            judged[row.plugin_id] = "min_host"
            continue
        if (
            row.api
            and row.api.isdigit()
            and int(row.api)
            not in (
                api_version,
                api_version - 1,
            )
        ):
            judged[row.plugin_id] = "api"
    return judged


def announce_incompatible(engine: Engine, bus: Any, judged: dict[str, str]) -> list[str]:
    """Tell `plugin.incompatible` once per (plugin, reason): the events already told are read
    back first, so a host that starts twice says nothing twice, and each one is handed to the
    subscribers (`deliver`, like every event the bus owes). Returns the keys it told."""
    with engine.connect() as connection:
        told = {
            (row.payload.get("plugin"), row.payload.get("reason"))
            for row in connection.execute(
                sa.select(platform.event.c.payload).where(
                    platform.event.c.type == "plugin.incompatible"
                )
            )
        }
    announced: list[str] = []
    published: list[Any] = []
    for key, reason in sorted(judged.items()):
        if (key, reason) in told:
            continue
        with write_transaction(engine) as connection:
            published.append(
                bus.publish(connection, "plugin.incompatible", {"plugin": key, "reason": reason})
            )
        announced.append(key)
    bus.deliver(published)
    return announced
