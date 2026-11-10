"""The adoption exits over the services that own them (EF-INT-023, L29 / L30 / L54).

`adoption_refused` (a shelf that looks fed by Suwayomi) and `coverage_unknown` (an adopted series
whose chapters cannot be told) are written down by the adoption and pairing services
(`waiting_marks`, one `app_setting` row each), so `status` shows them from any process. Leaving
them is the job of `LibraryOps`: the confirmed switch-over, or the watermark given by hand. This
class holds nothing of its own: it reads the marks and forwards the two commands.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.export.application import waiting_marks
from shelfhold.infrastructure.schema import export
from shelfhold.ops.library import LibraryOps
from shelfhold.ops.ports import WaitingItem
from shelfhold.ops.status import command_for

__all__ = ["LibraryAdoptionExit"]


class LibraryAdoptionExit:
    def __init__(self, engine: Engine, library: LibraryOps) -> None:
        self._engine = engine
        self._library = library

    def waiting(self) -> list[WaitingItem]:
        with self._engine.connect() as connection:
            marks = waiting_marks.read_all(connection)
            targets = {
                row.id: row.target_id
                for row in connection.execute(
                    sa.select(export.shelf_destination.c.id, export.shelf_destination.c.target_id)
                )
            }
        items = []
        for m in marks:
            if m.state == "mount_sentinel_missing":  # its exit is the recheck of the target
                command, reserved = command_for(m.state, m.subject, targets.get(m.subject, ""))
                items.append(WaitingItem(m.state, "export", m.subject, m.reason, command, reserved))
            elif m.state == "tombstone_journal_truncated":  # the root is the subject
                command, reserved = command_for(m.state, m.subject)
                items.append(
                    WaitingItem(m.state, "tombstones", m.subject, m.reason, command, reserved)
                )
            else:
                items.append(WaitingItem(m.state, "export", m.subject, m.reason))
        return items

    def set_follow_from(self, *, destination_id: str, series: str, chapter: str) -> None:
        self._library.set_follow_from(destination_id=destination_id, series=series, chapter=chapter)

    def confirm_migration(self, *, destination_id: str, fingerprint: str) -> None:
        self._library.confirm_migration(destination_id=destination_id, fingerprint=fingerprint)
