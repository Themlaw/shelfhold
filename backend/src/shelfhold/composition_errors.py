"""What the composition refuses with, apart from the services it builds."""


class NoLibraryError(RuntimeError):
    """No writable physical root is registered (no shelf was ever created)."""


class KeyringUnavailableError(RuntimeError):
    """The keyring is missing or unreadable: tombstones and sealed secrets cannot be opened."""
