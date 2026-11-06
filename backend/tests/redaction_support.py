"""Test support for the secret scrubber: the registry of known values is process-wide."""

from shelfhold.infrastructure import logging as shelfhold_logging


def forget_secret_values() -> None:
    shelfhold_logging._known_values.clear()
