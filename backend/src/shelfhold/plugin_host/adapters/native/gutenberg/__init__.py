"""Project Gutenberg adapter: the only code that knows the catalogue and the mirror layout."""

from shelfhold.plugin_host.adapters.native.gutenberg.source import GutenbergSource, build_manifest

__all__ = ["GutenbergSource", "build_manifest"]
