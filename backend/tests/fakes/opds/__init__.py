"""Synthetic OPDS documents in the shape Kavita and Komga serve them (M2 L22).

Nothing here is copied from a real server or a real book: the titles, authors and identifiers are
made up. The builders return bytes so a test serves them as a server would.
"""

from tests.fakes.opds.feeds import (
    ATOM,
    OPDS2,
    OPENSEARCH,
    acquisition_entry,
    atom_feed,
    entry_document,
    kavita_description,
    komga_description,
    navigation_entry,
    opds2_feed,
    publication,
)

__all__ = [
    "ATOM",
    "OPDS2",
    "OPENSEARCH",
    "acquisition_entry",
    "atom_feed",
    "entry_document",
    "kavita_description",
    "komga_description",
    "navigation_entry",
    "opds2_feed",
    "publication",
]
