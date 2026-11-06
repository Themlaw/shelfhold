"""Job ledger failures: `error_code` values written by the Huey adapter and the window scheduler."""

from shelfhold.error_catalog import ErrorCode

CODES: tuple[ErrorCode, ...] = (
    ErrorCode("job.unknown_kind", 500, "admin"),
    ErrorCode("job.failed", 500, "admin"),
    ErrorCode("window.preempted", 503, "admin"),
)


class JobFailedError(Exception):
    """A handler's refusal under a catalog code: the ledger keeps that code (and nothing else of
    the message, which may name a title), not the generic `job.failed`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
