"""Catalog: codes of the attach by external identifier (EF-BIB-003, EF-BIB-037, L12)."""

from shelfhold.error_catalog import ErrorCode

CODES: tuple[ErrorCode, ...] = (
    # shown: an Edition was attached to the Work that holds its identifier; "Séparer" undoes it
    ErrorCode("edition.auto_attached", 409, "admin", event=True),
    # never automatic: the attach would raise the age of the Work, an admin confirms the plan
    ErrorCode("edition.attach_suggested", 409, "admin", event=True),
    ErrorCode("edition.attach_refused", 409, "admin"),
)
