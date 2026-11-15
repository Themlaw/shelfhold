"""The read state a server keeps itself (Kavita's pull, L14b): an internal door.

A reader's report (`progress.record_position`) never makes a Unit read by itself; the state a
server holds does, because the server is the one that knows what it showed (and the member can
still undo it, `progress.undo_read_mark`). That power must not be reachable by whoever calls the
public service: no interface (route, device token, kosync, OPDS) imports this module, an
architecture test (`test_server_state_door_is_closed_to_interfaces`) and an import contract hold
it, and it is not in `reading.public`.

It accepts only a **linked, live** account of the member on the destination
(`member_keys.live_member_account`, `revoked_at` read at each call), and notes the destination the
state came from (`device_id = "server:<destination>"`).

Contract for L14b (the push and the pull of Kavita):

* Shelfhold **notes what it pushes** to Kavita (which chapter, which page, read or not, and when);
* the echo of its own push is **never read back as a `server` read**: a pulled state equal to
  what was pushed is dropped. Otherwise a look at the last page of a chapter (a `device`
  position), pushed to Kavita as « furthest », which marks it read there, would come back as a
  trusted « read » and wash the look into a reading;
* the last page of a chapter is pushed as read only when a traversal marked the Unit
  (`reading_progress.state = 'read'`), else Kavita is given the page, not the mark.
"""

from datetime import datetime
from typing import Any

import sqlalchemy as sa

from shelfhold.reading.application.member_keys import live_member_account
from shelfhold.reading.application.progress import check_carrier, insert_position

__all__ = ["record_server_state"]


def record_server_state(
    connection: sa.Connection,
    *,
    user_id: str,
    shelf_destination_id: str,
    carrier_type: str,
    carrier_id: str,
    content_hash: str,
    locator: dict[str, Any],
    percent: float | None,
    client_ts: datetime,
    now: datetime | None = None,
) -> str:
    """Record a state pulled from a destination for a member whose account there is live."""
    live_member_account(connection, user_id=user_id, shelf_destination_id=shelf_destination_id)
    check_carrier(connection, user_id, carrier_type, carrier_id)
    return insert_position(
        connection, user_id=user_id, carrier_type=carrier_type, carrier_id=carrier_id,
        content_hash=content_hash, locator=locator, percent=percent, client_ts=client_ts,
        device_id=f"server:{shelf_destination_id}", source="server", now=now,
    )  # fmt: skip
