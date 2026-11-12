"""Operating failures: pauses and alerts whose code is also the event of the failure matrix."""

from shelfhold.error_catalog import ErrorCode

CODES: tuple[ErrorCode, ...] = (
    ErrorCode("auth.passkey_registered", 409, "admin", event=True),
    ErrorCode("backup.failed", 503, "admin", event=True),
    ErrorCode("backup.stale", 503, "admin", event=True),
    ErrorCode("disk.low", 503, "admin", event=True),
    ErrorCode("job.dead_lettered", 500, "admin", event=True),
    ErrorCode("secret.undecryptable", 503, "admin", event=True),
    ErrorCode("instance.lease_conflict", 503, "admin", event=True),
    ErrorCode("instance.restored_behind", 503, "admin", event=True),
    ErrorCode("notify.failed", 502, "admin", event=True),
    ErrorCode("mount.sentinel_missing", 503, "admin", event=True),
    ErrorCode("reconcile.drift", 409, "admin", event=True),
    ErrorCode("wake.deferred", 503, "admin", event=True),
    ErrorCode("metadata.provider_down", 503, "admin", event=True),
)
