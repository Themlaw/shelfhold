"""Catalog: codes of the attach by external identifier (EF-BIB-003, EF-BIB-037, L12) and of the
merge of Editions (EF-BIB-049, L14), and of the collections and reading lists (EF-BIB-016)."""

from shelfhold.error_catalog import ErrorCode

CODES: tuple[ErrorCode, ...] = (
    # shown: an Edition was attached to the Work that holds its identifier; "Séparer" undoes it
    ErrorCode("edition.auto_attached", 409, "admin", event=True),
    # never automatic: the attach would raise the age of the Work, an admin confirms the plan
    ErrorCode("edition.attach_suggested", 409, "admin", event=True),
    ErrorCode("edition.attach_refused", 409, "admin"),
    # EF-BIB-024 (L17): a new version far smaller than the stored one, or a stub, was quarantined;
    # the stored version stays exported
    ErrorCode("unit.revision_suspect", 409, "admin", event=True),
    # EF-BIB-043: the "to rewrite" queue is confirmed by batch, with the token `revisions list` gave
    ErrorCode("revisions.confirmation_required", 409, "admin"),
    # a merge of Editions that a tombstone, another Work or a dependent row stops (EF-BIB-049)
    ErrorCode("edition.merge_refused", 409, "admin"),
    # M3 L22 (EF-BIB-019): a merge of Works that its plan refuses, and "Séparer" refused
    ErrorCode("work.merge_refused", 409, "admin"),
    ErrorCode("merge.expired", 409, "admin"),
    ErrorCode("merge.already_undone", 409, "admin"),
    ErrorCode("merge.survivor_gone", 409, "admin"),
    ErrorCode("series.already_attached", 409, "admin"),  # EF-BIB-025: one Series at most
    # M4 L18a (EF-BIB-016): a reading-list file that is refused (never cut), a list that is full
    ErrorCode("collection.cbl_refused", 422, "user"),
    ErrorCode("collection.full", 409, "user"),
    # M4 L18b (EF-EXP-025): a household collection has no private list to approve for Kavita
    ErrorCode("collection.not_private", 409, "admin"),
    # M4 L19 (EF-BIB-039): a relation that cannot exist, and one that would close a loop
    ErrorCode("relation.invalid", 422, "admin"),
    ErrorCode("relation.cycle", 409, "admin"),
    ErrorCode("relation.limit", 422, "admin"),  # a Work holds at most 50 relations
)
