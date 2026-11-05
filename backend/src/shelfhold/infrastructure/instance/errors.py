"""One instance per volumes: every refusal carries `instance.lease_conflict` (02 section 9)."""

CODE = "instance.lease_conflict"


class InstanceError(RuntimeError):
    code = CODE


class InstanceLockHeldError(InstanceError):
    """Another process holds `/data/.lock`: this one must not start."""


class InstanceLockLinkError(InstanceLockHeldError):
    """`/data/.lock` is a symbolic link: never followed, never ignored."""


class LeaseConflictError(InstanceError):
    """A root's lease belongs to another instance that may still be writing."""


class LeaseLostError(InstanceError):
    """This session's lease was taken over (or ran out): no further write is allowed."""
