"""The CWA drop-box destination: an EPUB dropped into an ingestion folder (EF-EXP-022)."""

from shelfhold.export.targets.cwa.destination import CwaDestination, DropSpec, drop_name
from shelfhold.export.targets.cwa.source import KIND, CwaSource

__all__ = ["KIND", "CwaDestination", "CwaSource", "DropSpec", "drop_name"]
