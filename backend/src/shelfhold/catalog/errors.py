"""Catalog: codes of the attach by external identifier (EF-BIB-003, EF-BIB-037, L12)."""

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
)
