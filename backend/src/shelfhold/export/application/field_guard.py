"""The `field_owner` decision applied to a Kavita field, with its `reconcile.drift` (EF-EXP-057).

Only the fields Shelfhold locked itself are rewritten; a field locked by a third party (the admin,
Kavita+) is left alone; a field of the registry that shows another value than the last push is
drift: announced, and pushed again instead of a silent abstention.
"""

from __future__ import annotations

from typing import Any

from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.application.target_state import FieldRegistry
from shelfhold.export.domain.field_owner import FieldAction, decide, is_drift

__all__ = ["FieldGuard"]


class FieldGuard:
    def __init__(self, registry: FieldRegistry, events: TargetEvents) -> None:
        self._registry = registry
        self._events = events

    def decide(
        self,
        destination_id: str,
        series_id: str,
        field: str,
        *,
        locked_in_kavita: bool,
        kavita_value: Any,
    ) -> FieldAction:
        action = decide(
            locked_in_kavita=locked_in_kavita,
            kavita_value=kavita_value,
            owned=self._registry.get(destination_id, series_id, field),
        )
        if is_drift(action):
            self._events.emit(
                "reconcile.drift", cause="field_modified_in_kavita", destination_id=destination_id,
                export_series_id=series_id, field=field,
            )  # fmt: skip
        return action
