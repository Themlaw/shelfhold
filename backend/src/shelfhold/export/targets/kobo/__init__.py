"""The Kobo folder destination: a tree of `.kepub.epub` for a mounted reader (EF-EXP-021, D19)."""

from shelfhold.export.targets.kobo.destination import BookSpec, KoboDestination, SeriesSpec
from shelfhold.export.targets.kobo.names import KEPUB_EXT, book_filename, series_folder
from shelfhold.export.targets.kobo.source import KIND, KoboSource, kobo_series

__all__ = [
    "KEPUB_EXT",
    "KIND",
    "BookSpec",
    "KoboDestination",
    "KoboSource",
    "SeriesSpec",
    "book_filename",
    "kobo_series",
    "series_folder",
]
