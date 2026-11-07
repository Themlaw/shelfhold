"""A Series is optional above a Work, and can come later (EF-BIB-025).

Attaching one writes the membership and nothing else: the Work, its Editions, its Units and the
files already exported are not touched. What an exported series becomes is the business of an
explicit plan that moves the link of an Edition (EF-EXP-062), never of this call.
"""

from shelfhold.catalog.application.ports import WorkStore

__all__ = ["SeriesAlreadyAttachedError", "attach_work"]


class SeriesAlreadyAttachedError(Exception):
    """A Work belongs to at most one Series."""


def attach_work(store: WorkStore, series_id: str, work_id: str, position: str) -> None:
    if store.series_of(work_id) is not None:
        raise SeriesAlreadyAttachedError(work_id)
    store.insert_member(series_id, work_id, position)
