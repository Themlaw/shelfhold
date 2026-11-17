"""The export facade: the only door other contexts use (import-linter `contexts-independent`)."""

from sqlalchemy import Connection

from shelfhold.export.application.coverage import adopted_volumes, attach_to_volume
from shelfhold.export.application.export_items import (
    SeriesMove,
    SeriesReassignmentPlan,
    SeriesReassignmentRefusedError,
    apply_series_reassignment,
    plan_series_reassignment,
)
from shelfhold.export.application.pairing import pending_pairing
from shelfhold.export.application.upgrade import served_blockers
from shelfhold.export.application.waiting_marks import clear as _clear_marks
from shelfhold.export.domain.bindings import (
    LocalSourceUnknownError,
    RootOverlapsLocalSourceError,
    adult_clause,
    family_clause,
    is_family,
)
from shelfhold.export.domain.folders import Relation, absolute_relation, folder_relation
from shelfhold.export.domain.path_map import PathMapError, clean_absolute

__all__ = [
    "LocalSourceUnknownError",
    "PathMapError",
    "Relation",
    "RootOverlapsLocalSourceError",
    "SeriesMove",
    "SeriesReassignmentPlan",
    "SeriesReassignmentRefusedError",
    "absolute_relation",
    "adopted_volumes",
    "adult_clause",
    "apply_series_reassignment",
    "attach_to_volume",
    "clean_absolute",
    "family_clause",
    "folder_relation",
    "forget_coverage_unknown",
    "is_family",
    "pending_pairing",
    "plan_series_reassignment",
    "served_blockers",
]


def forget_coverage_unknown(connection: Connection, export_series_id: str) -> None:
    """The admin declined the pairing: the series no longer waits for a coverage."""
    _clear_marks(connection, "coverage_unknown", [export_series_id])
