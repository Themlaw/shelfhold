"""Errors of the secrets layer. Messages never carry key material or plaintext."""


class SecretsError(Exception):
    """Base of every error raised by `shelfhold.infrastructure.secrets`."""


class EnvelopeError(SecretsError):
    """The envelope cannot be opened."""


class EnvelopeFormatError(EnvelopeError):
    """The envelope is malformed (missing field, bad encoding, wrong nonce or tag length)."""


class UnsupportedVersionError(EnvelopeError):
    def __init__(self, version: object) -> None:
        super().__init__(f"unsupported envelope format version: {version!r}")
        self.version = version


class UnsupportedAlgorithmError(EnvelopeError):
    def __init__(self, alg: object) -> None:
        super().__init__(f"unsupported envelope algorithm: {alg!r}")
        self.alg = alg


class UnknownKidError(EnvelopeError):
    """The key id is not in the keyring (restore a newer keyring or import the last export)."""

    def __init__(self, kid: str) -> None:
        super().__init__(f"unknown key id: {kid!r}")
        self.kid = kid


class DecryptionError(EnvelopeError):
    """Authentication failed: altered, truncated, or bound to another row, or the wrong key."""

    def __init__(self) -> None:
        super().__init__("the secret does not authenticate (altered, moved or wrong key)")


class KeyringError(SecretsError):
    """The keyring file cannot be used."""


class KeyringUnreadableError(KeyringError):
    """The process uid cannot read the keyring: the application must not start (ENF-SEC-005)."""

    def __init__(self, path: object, cause: str) -> None:
        super().__init__(f"cannot read the keyring {path}: {cause}")
        self.path = path


class KeyringPermissionsError(KeyringError):
    """The keyring is group/world accessible or owned by another uid: refuse, say how to fix."""

    def __init__(self, path: object, reason: str) -> None:
        super().__init__(
            f"the keyring {path} {reason}; fix it with: chmod 600 {path} "
            f"(and chown the file to the user running Shelfhold)"
        )
        self.path = path


class KeyringCorruptError(KeyringError):
    def __init__(self, path: object, detail: str) -> None:
        super().__init__(f"the keyring {path} is not usable: {detail}")
        self.path = path


class KeyringMissingError(KeyringError):
    """No keyring where one must exist: generating another would orphan the sealed secrets."""

    def __init__(self, path: object, *, reason: str) -> None:
        super().__init__(f"no keyring at {path} ({reason})")
        self.path = path
        self.reason = reason


class KeyConflictError(KeyringError):
    """An imported key has the id of a key we hold, with other material: the import is refused
    whole (a merge never overwrites). Names the id, never a byte of either key."""

    def __init__(self, kid: str) -> None:
        super().__init__(f"key {kid!r} exists in the keyring with other material")
        self.kid = kid


class KeyRetirementRefusedError(KeyringError):
    """`reason`: unknown, active, in_use, cited_by_tombstone, root_unavailable, not_retired."""

    def __init__(self, kid: str, reason: str) -> None:
        super().__init__(f"key {kid!r} cannot be retired: {reason}")
        self.kid = kid
        self.reason = reason


class RotationInProgressError(SecretsError):
    """Another `secrets rotate` holds the lock."""


class ExportError(SecretsError):
    """The passphrase or the export file is unusable."""


class WorkFactorError(ExportError):
    """An age file asks for more scrypt work than this program accepts to do (`MAX_LOG_N`). It is
    not a wrong passphrase, and the message says so."""
