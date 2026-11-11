"""The generic OPDS source (EF-SRC-006): the only code that knows OPDS, OpenSearch and the shapes
Kavita and Komga give them. It imports nothing of the host but the SDK."""

from shelfhold.plugin_host.adapters.native.opds.source import (
    KEY,
    MAX_PAGES,
    NotConfigured,
    OpdsSource,
    build_manifest,
)

__all__ = ["KEY", "MAX_PAGES", "NotConfigured", "OpdsSource", "build_manifest"]
