# SPDX-License-Identifier: MIT
"""Deprecated re-export of `shelfhold_sdk` (ADR-0024, D10).

Since M6 the SDK is its own distribution, `shelfhold-sdk` (MIT), imported as `shelfhold_sdk`:
a separate distribution cannot provide `shelfhold.sdk`, and a `shelfhold` package shipped by a
plugin would collide with the host. This package stays during the whole 1.x series of the
application and re-exports every `shelfhold_sdk` module under its old name, so code written
for the 1.x series keeps importing `shelfhold.sdk.*` unchanged; it disappears with `api` 1.

The pytest plugin (`shelfhold_sdk.testing.pytest_plugin`) is deliberately NOT re-exported:
it is the only SDK module that imports `pytest`, which the production image does not carry.
"""

import sys

import shelfhold_sdk
import shelfhold_sdk.age
import shelfhold_sdk.api_call
import shelfhold_sdk.connected
import shelfhold_sdk.events
import shelfhold_sdk.export_target
import shelfhold_sdk.fetcher
import shelfhold_sdk.fsops
import shelfhold_sdk.io_guard
import shelfhold_sdk.lang
import shelfhold_sdk.metadata
import shelfhold_sdk.models
import shelfhold_sdk.refs
import shelfhold_sdk.source
import shelfhold_sdk.testing
import shelfhold_sdk.testing.corpus
import shelfhold_sdk.testing.corpus.trapped_epubs
import shelfhold_sdk.testing.corpus.trapped_epubs.build
import shelfhold_sdk.testing.corpus.trapped_epubs.cases
import shelfhold_sdk.testing.corpus.trapped_epubs.detect
import shelfhold_sdk.testing.corpus.trapped_epubs.standard
import shelfhold_sdk.testing.destination_kit
import shelfhold_sdk.testing.fakes
import shelfhold_sdk.testing.fixtures
import shelfhold_sdk.testing.fixtures.reserved_targets
import shelfhold_sdk.testing.kit  # noqa: F401  (aliased into sys.modules below)

# `shelfhold_sdk.testing.pytest_plugin` is not imported above on purpose: it is the only SDK
# module that needs `pytest`, which the production image does not carry, and this re-export
# must stay importable without it.
for _name, _module in list(sys.modules.items()):
    if _name == "shelfhold_sdk" or _name.startswith("shelfhold_sdk."):
        _alias = "shelfhold.sdk" + _name[len("shelfhold_sdk") :]
        sys.modules.setdefault(_alias, _module)
        _parent_name, _, _leaf = _alias.rpartition(".")
        _parent = sys.modules.get(_parent_name)
        if _parent is not None:
            setattr(_parent, _leaf, _module)  # `from shelfhold.sdk import models`

__all__ = [
    "age",
    "api_call",
    "connected",
    "events",
    "export_target",
    "fetcher",
    "fsops",
    "io_guard",
    "lang",
    "metadata",
    "models",
    "refs",
    "source",
    "testing",
]
