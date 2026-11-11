"""Wikisource adapter: the only code that knows the MediaWiki API and the export tool."""

from shelfhold.plugin_host.adapters.native.wikisource.source import WikisourceSource, build_manifest

__all__ = ["WikisourceSource", "build_manifest"]
