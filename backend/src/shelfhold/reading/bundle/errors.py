"""What can go wrong when a block is served. None of these carries a title or a path: the route
answers with a stable code, and the reason stays in the log."""


class BundleNotFoundError(LookupError):
    """No such block for this reader: hidden, unknown, someone else's, or a part that is not there.
    The route answers like for a thing that does not exist."""


class BundleGoneError(Exception):
    """The block cannot be served any more (410): a file changed, a version was purged, the
    token was revoked, the reader was below the minimal rate. Nothing more is sent."""


class BundleChangedError(BundleGoneError):
    """A file does not hold what the manifest said: replaced under the reader, or overwritten in
    place during the transfer. The stream stops rather than mixing two contents."""


class BundleBusyError(Exception):
    """Too many blocks are being served (429); `retry_after_s` is an honest wait."""

    def __init__(self, retry_after_s: int) -> None:
        super().__init__(f"busy, retry in {retry_after_s}s")
        self.retry_after_s = max(1, int(retry_after_s))
