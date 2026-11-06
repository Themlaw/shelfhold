"""Versions a tombstone may cite, which never leave the code (ENF-OPS-021, EF-BIB-054 (3)).

A tombstone records the `normalizer_id@version` and the `content_hash_by` in force at the purge.
Any version that a release has shipped may be cited by a tombstone on some installation, so the
list below only grows: adding a version appends it, and removing a version from the code makes
`test_cited_normalizer_version_never_removed` fail in CI. At run time the index refuses to build
when a version cited by a stored tombstone is unknown (`CitedVersionMissingError`).
"""

from typing import Final

from shelfhold.catalog.public import NormalizerId, NormalizerRegistry, load_registry
from shelfhold.library.domain.content_hash import CONTENT_HASH_BY
from shelfhold.library.tombstones.index import RegistryNormalizers

# Append only (never edit or remove a line).
CITABLE_NORMALIZERS: Final[tuple[str, ...]] = (
    "series_name@1",
    "team_label@1",
    "text_version@1",
    "number@1",
    "lang@1",
    "publisher@1",
    "number_segment@1",
    "remote_unit_id@1",
)
CITABLE_CONTENT_HASHES: Final[tuple[str, ...]] = ("content-hash@1",)
SUPPORTED_CONTENT_HASHES: Final[frozenset[str]] = frozenset({CONTENT_HASH_BY})


def normalizers_of(registry: NormalizerRegistry) -> RegistryNormalizers:
    return RegistryNormalizers(
        current=lambda name: str(registry.current(name)),
        function_of=lambda text: registry.get(NormalizerId.parse(text)),
    )


def default_normalizers() -> RegistryNormalizers:
    """The versions in the code, and the ones a first-party distribution declares (a tombstone
    may cite either)."""
    return normalizers_of(load_registry())


def missing_citable(
    registry: NormalizerRegistry,
    *,
    content_hashes: frozenset[str] = SUPPORTED_CONTENT_HASHES,
    citable: tuple[str, ...] = CITABLE_NORMALIZERS,
    citable_hashes: tuple[str, ...] = CITABLE_CONTENT_HASHES,
) -> list[str]:
    """Citable versions the code no longer provides (empty in a sound build)."""
    adapter = normalizers_of(registry)
    missing = [version for version in citable if not adapter.known(version)]
    missing += [by for by in citable_hashes if by not in content_hashes]
    return missing
