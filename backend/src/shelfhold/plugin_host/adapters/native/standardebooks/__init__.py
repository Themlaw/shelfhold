"""Standard Ebooks adapter: the only code that knows the layout of a book and the patron feed."""

from shelfhold.plugin_host.adapters.native.standardebooks.source import (
    StandardEbooksSource,
    build_manifest,
)

__all__ = ["StandardEbooksSource", "build_manifest"]
