"""Plugin of tests/conftest.py: the logging configured by a test does not outlive it.

`configure_logging` binds a handler to the stream of the moment (so that a test can capture it).
Once the test is over that stream is closed, and the next line a background thread, or the next
test, writes through the handler fails with `ValueError: I/O operation on closed file`, whose
`--- Logging error ---` and `Traceback` land in the output of whoever runs then. Structlog
unconfigured prints every level on stdout: it is put back to the defaults too.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
import structlog

from shelfhold.infrastructure.logging import _HANDLER_MARK


@pytest.fixture(autouse=True)
def _logging_does_not_outlive_the_test() -> Iterator[None]:
    yield
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(handler)
    structlog.reset_defaults()
