"""The fingerprint of the connected service's installation (EF-SRC-036, EF-INT-023).

`source_config.instance_fingerprint` is what the service said it is the first time it said
anything. Another answer later means the tool was installed again: the follow pauses
(`source_instance_changed`), and nothing else happens. No binding is deactivated, no Unit becomes
irreplaceable, no ticket is dropped and nothing is asked again (the cascade of EF-SRC-027 is for a
series that left a library that is otherwise there). `source reconnect` is the way out: it forgets
the old fingerprint, so the next cycle reads the new installation as the reference, and the
identities, which are the upstream's, find their way back by themselves.
"""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.acquisition.application.failures import FailureLedger
from shelfhold.events.waiting_state import WaitingState
from shelfhold.infrastructure.schema import acquisition, catalog

__all__ = ["check_instance", "reconnect"]

_CONFIG = acquisition.source_config


def check_instance(connection: Connection, plugin_id: str, current: str, now: datetime) -> bool:
    """True when `current` is not the installation on record. A first look records it (when the
    service can say; `""` is "not yet"); a look that agrees only refreshes the date seen."""
    connection.execute(
        sqlite_insert(_CONFIG)
        .values(plugin_id=plugin_id, health={})
        .on_conflict_do_nothing(index_elements=["plugin_id"])
    )
    stored = connection.execute(
        sa.select(_CONFIG.c.instance_fingerprint).where(_CONFIG.c.plugin_id == plugin_id)
    ).scalar_one()
    if stored is None:
        if current:
            _write(connection, plugin_id, current, now)
        return False
    if stored != current:
        return True
    _write(connection, plugin_id, current, now)
    return False


def reconnect(connection: Connection, plugin_id: str, now: datetime) -> bool:
    """`source reconnect`: leave the pause that another installation caused, and only that one.
    Returns False when the service is not paused for it (another pause waits for its own exit).

    The tickets still open on the old installation and the cached `fetch_handle`s are purged
    here (EF-SRC-036 (3)), not at the detection, which touches nothing: the tickets go back to
    `requested` with no ticket (the cycle asks the new installation again for what is still
    wanted; no Unit, Variant or file is touched) and the handles are forgotten (read again by
    `(sourceId, chapter.url)`)."""
    ledger = FailureLedger(plugin_id)
    pause = ledger.paused(connection)
    if pause is None or pause.waiting_state != WaitingState.SOURCE_INSTANCE_CHANGED:
        return False
    connection.execute(
        sa.update(_CONFIG)
        .where(_CONFIG.c.plugin_id == plugin_id)
        .values(instance_fingerprint=None, instance_fingerprint_seen_at=None)
    )
    _purge_orphans(connection, plugin_id, now)
    ledger.clear_mass_alert(connection)
    ledger.resume(connection)
    return True


def _purge_orphans(connection: Connection, plugin_id: str, now: datetime) -> None:
    bindings = (
        sa.select(acquisition.source_binding.c.id)
        .join(
            acquisition.source_item,
            acquisition.source_item.c.id == acquisition.source_binding.c.source_item_id,
        )
        .where(acquisition.source_item.c.plugin_id == plugin_id)
    )
    request = acquisition.download_request
    connection.execute(
        sa.update(request)
        .where(
            request.c.source_binding_id.in_(bindings),
            request.c.state.in_(("requested", "downloaded")),
            request.c.ticket.is_not(None),
        )
        .values(ticket=None, state="requested", attempts=0, updated_at=now)
    )
    variant = catalog.unit_variant
    connection.execute(
        sa.update(variant)
        .where(variant.c.source_binding_id.in_(bindings))
        .values(fetch_handle=None, fetch_handle_seen_at=None)
    )


def _write(connection: Connection, plugin_id: str, fingerprint: str, now: datetime) -> None:
    connection.execute(
        sa.update(_CONFIG)
        .where(_CONFIG.c.plugin_id == plugin_id)
        .values(instance_fingerprint=fingerprint, instance_fingerprint_seen_at=now)
    )
