"""The orphaned mark of a binding whose plugin went away (EF-SRC-020, D15, M6 L09a).

Uninstalling a third-party plugin takes its code away, never what it brought: the Work, its
Edition, the Units, the ``export_item`` rows and the reading positions stay, and the binding
that tied the Edition to the plugin's source is *marked* — ``active = false`` with the reason
``orphaned`` — not deleted. Nothing is fetched for an orphaned binding (it is inactive), and a
reinstall of the same key at the same signer reattaches it: the binding goes back to active,
unless another binding took its rank in the Edition meanwhile (the rank is unique among the
active ones; M1b decides between them, not this mark).

The caller owns the transaction; the uninstall and the reinstall are the host's (the plugin
lifecycle, a session of an admin or the local CLI).
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.schema import acquisition

__all__ = ["orphan_bindings", "reattach_orphaned"]

ORPHANED = "orphaned"


def _of_plugin(plugin_id: str, *columns: Any) -> sa.Select[Any]:
    """The plugin's bindings: the columns the caller asks of them, joined to the plugin's
    sources (the binding itself says nothing about the plugin — the source item does)."""
    item, binding = acquisition.source_item.c, acquisition.source_binding.c
    return (
        sa.select(*columns)
        .join(acquisition.source_item, item.id == binding.source_item_id)
        .where(item.plugin_id == plugin_id)
    )


def orphan_bindings(connection: Connection, plugin_id: str) -> int:
    """Mark every active binding of the plugin's sources orphaned. The rows stay, with all they
    hold; how many were marked is the caller's to tell the admin."""
    binding = acquisition.source_binding.c
    return connection.execute(
        sa.update(acquisition.source_binding)
        .where(
            binding.id.in_(_of_plugin(plugin_id, binding.id)),
            binding.active.is_(True),
        )
        .values(active=False, disabled_reason=ORPHANED)
    ).rowcount


def reattach_orphaned(connection: Connection, plugin_id: str) -> int:
    """Reactivate the plugin's orphaned bindings, each unless another active binding took its
    rank in the Edition meanwhile (the partial unique index would refuse it, and the rank is
    M1b's to arbitrate, not the reinstall's)."""
    binding = acquisition.source_binding.c
    reattached = 0
    for row in connection.execute(
        _of_plugin(plugin_id, binding.id, binding.edition_id, binding.priority).where(
            binding.disabled_reason == ORPHANED
        )
    ).all():
        taken = connection.execute(
            sa.select(sa.literal(1)).where(
                binding.edition_id == row.edition_id,
                binding.priority == row.priority,
                binding.active,
                binding.id != row.id,
            )
        ).first()
        if taken is not None:  # another binding serves the Edition at that rank now
            continue
        reattached += connection.execute(
            sa.update(acquisition.source_binding)
            .where(binding.id == row.id, binding.disabled_reason == ORPHANED)
            .values(active=True, disabled_reason=None)
        ).rowcount
    return reattached
