"""Job ledger failures: `error_code` values written by the Huey adapter and the window scheduler."""

from shelfhold.error_catalog import ErrorCode

CODES: tuple[ErrorCode, ...] = (
    ErrorCode("job.unknown_kind", 500, "admin"),
    ErrorCode("job.failed", 500, "admin"),
    ErrorCode("window.preempted", 503, "admin"),
    # EF-INT-018: every write is refused while the mode is on; the only way out is `maintenance off`
    ErrorCode("maintenance.active", 503, "admin"),
    # the database could not be read just now (locked): not the mode, and nothing to leave
    ErrorCode("db.busy", 503, "admin"),
    # EF-INT-017: a security repair keeps its place above every class a user may choose
    ErrorCode("queue.priority_locked", 409, "admin"),
)


class JobFailedError(Exception):
    """A handler's refusal under a catalog code: the ledger keeps that code (and nothing else of
    the message, which may name a title), not the generic `job.failed`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
