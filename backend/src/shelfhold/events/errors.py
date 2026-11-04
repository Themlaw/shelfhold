"""Operating failures: pauses and alerts whose code is also the event of the failure matrix."""

from shelfhold.error_catalog import ErrorCode

CODES: tuple[ErrorCode, ...] = (
    ErrorCode("backup.failed", 503, "admin"),
    ErrorCode("backup.stale", 503, "admin"),
    ErrorCode("disk.low", 503, "admin"),
    ErrorCode("job.dead_lettered", 500, "admin"),
    ErrorCode("secret.undecryptable", 503, "admin"),
    ErrorCode("instance.lease_conflict", 503, "admin"),
    ErrorCode("instance.restored_behind", 503, "admin"),
    ErrorCode("notify.failed", 502, "admin"),
    ErrorCode("mount.sentinel_missing", 503, "admin"),
    ErrorCode("reconcile.drift", 409, "admin"),
    ErrorCode("wake.deferred", 503, "admin"),
    ErrorCode("metadata.provider_down", 503, "admin"),
)
