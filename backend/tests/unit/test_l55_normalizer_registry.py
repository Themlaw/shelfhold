"""L55 (ENF-OPS-021): what a release may add to the registry, and what is never imported."""

from importlib.metadata import EntryPoint

import pytest

from shelfhold.catalog.public import NormalizerDeclaration, default_registry, load_registry

pytestmark = pytest.mark.req("ENF-OPS-021")


def upper(raw: str) -> str:
    return raw.upper()


def test_a_declaration_adds_the_next_version_of_an_existing_normalizer() -> None:
    registry = default_registry()
    refused = registry.extend([NormalizerDeclaration("number", 2, upper)])
    assert refused == [] and str(registry.current("number")) == "number@2"
    assert registry.normalize("number", "x").by == "number@2"


@pytest.mark.parametrize(
    ("declaration", "reason"),
    [
        (NormalizerDeclaration("brand_new", 1, upper), "unknown_normalizer"),
        (NormalizerDeclaration("number", 1, upper), "already registered"),
        (NormalizerDeclaration("number", 3, upper), "skips a version"),
    ],
)
def test_a_declaration_cannot_add_a_name_replace_a_version_or_skip_one(
    declaration: NormalizerDeclaration, reason: str
) -> None:
    registry = default_registry()
    before = registry.names()
    refused = registry.extend([declaration])
    assert [(d.name, d.version) for d, _ in refused] == [(declaration.name, declaration.version)]
    assert reason in refused[0][1]
    assert registry.names() == before and str(registry.current("number")) == "number@1"


def test_an_entry_point_with_no_distribution_is_never_loaded() -> None:
    stranger = EntryPoint("x", "os:system", "shelfhold.normalizers")  # would be a disaster
    assert str(load_registry([stranger]).current("number")) == "number@1"
