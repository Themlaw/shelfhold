"""Failure classes and what each one does (02 section 3.4, EF-SRC-009, EF-SRC-028).

An error of a connected service belongs to one class, and the class alone decides the outcome:
only `content` is counted (it can make a Unit irreplaceable, EF-BIB-042); `auth`, `version`,
`schema` and `instance` mean the service itself cannot be driven, so the whole service pauses
and nothing is counted against any series; `infra` (asleep, rate-limited, truncated, reset) is
retried later;
`untrusted` (a file with an entry off the allowlist, EF-BIB-051 (6)) is neither: the same bytes
would be refused again, and the source did nothing a failure count should answer for.
"""

from enum import StrEnum

from shelfhold.plugin_host.errors import failure_class_of


class Disposition(StrEnum):
    COUNT = "count"  # one more consecutive content error for this source
    REFUSE = "refuse"  # the file is refused for good: closed, announced, never retried, not counted
    PAUSE = "pause"  # the service waits for a human; nothing is counted
    RETRY = "retry"  # try again next cycle; nothing is counted


_DISPOSITIONS = {
    "content": Disposition.COUNT,
    "untrusted": Disposition.REFUSE,
    "auth": Disposition.PAUSE,
    "version": Disposition.PAUSE,
    "schema": Disposition.PAUSE,
    "instance": Disposition.PAUSE,  # another installation of the tool (EF-SRC-036)
    "infra": Disposition.RETRY,
}


def classify(error: BaseException) -> str:
    """`content`, `auth`, `version`, `schema` or `infra`.

    A `KeyError` or `TypeError` is `infra` here: raised by Shelfhold's own code it is a bug, and
    a bug must not pause a service. Only what comes out of a service call is read as a schema
    change, by `from_service`, which wraps it in `SourceChanged`."""
    return failure_class_of(error)


def disposition_of(failure_class: str) -> Disposition:
    return _DISPOSITIONS[failure_class]
