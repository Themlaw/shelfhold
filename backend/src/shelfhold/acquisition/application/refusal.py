"""The refusal of an acquisition under a catalog code (shared by `acquire` and `describe`)."""

__all__ = ["AcquireRefused"]


class AcquireRefused(Exception):
    """A refusal under a catalog code (never the text of the source)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
