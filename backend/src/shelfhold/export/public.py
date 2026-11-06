"""The export facade: the only door other contexts use (import-linter `contexts-independent`)."""

from sqlalchemy import Connection

from shelfhold.export.application.coverage import attach_to_volume
from shelfhold.export.application.pairing import pending_pairing
from shelfhold.export.application.waiting_marks import clear as _clear_marks

__all__ = ["attach_to_volume", "forget_coverage_unknown", "pending_pairing"]


def forget_coverage_unknown(connection: Connection, export_series_id: str) -> None:
    """The admin declined the pairing: the series no longer waits for a coverage."""
    _clear_marks(connection, "coverage_unknown", [export_series_id])
