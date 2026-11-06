"""The export facade: the only door other contexts use (import-linter `contexts-independent`)."""

from shelfhold.export.application.pairing import pending_pairing

__all__ = ["pending_pairing"]
