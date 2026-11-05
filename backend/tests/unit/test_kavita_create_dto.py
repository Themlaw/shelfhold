"""The library a shelf creates is valid for every Kavita version of the range (EF-EXP-009).

Found by the nightly against a real Kavita: `metadataProvider` is optional in the schema but its
default 0 is outside the enum (400) and the provider must suit the library type (500).
"""

import json
from pathlib import Path
from typing import Any

import pytest

from shelfhold.export.targets.kavita import dto

OPENAPI = Path(__file__).resolve().parents[1] / "fixtures" / "kavita" / "openapi"


@pytest.mark.req("EF-EXP-009")
@pytest.mark.parametrize("version", ["0.9.1", "pinned", "latest"])
def test_create_dto_is_valid_against_the_published_schema(version: str) -> None:
    schemas: dict[str, Any] = json.loads((OPENAPI / f"{version}.json").read_text("utf-8"))[
        "components"
    ]["schemas"]
    schema = schemas["UpdateLibraryDto"]
    for kind in dto.LibraryType:
        body = dto.create_dto("Name", kind, ["/library/x"])
        assert set(schema["required"]) <= set(body)
        assert set(body) <= set(schema["properties"])
        assert body["metadataProvider"] in schemas["MetadataProvider"]["enum"]
        assert body["metadataProvider"] == (
            3 if kind in (dto.LibraryType.MANGA, dto.LibraryType.IMAGE) else 2
        )
        assert body["excludePatterns"] == list(dto.HIDDEN_DIR_PATTERNS)


@pytest.mark.req("EF-EXP-009")
@pytest.mark.parametrize("version", ["0.9.1", "pinned", "latest"])
def test_library_filter_names_the_libraries_field_of_the_published_schema(version: str) -> None:
    """Field 18 was Genres: the filter silently listed nothing on a real Kavita (nightly)."""
    schemas: dict[str, Any] = json.loads((OPENAPI / f"{version}.json").read_text("utf-8"))[
        "components"
    ]["schemas"]
    names = schemas["SeriesFilterField"]["x-enum-varnames"]
    values = schemas["SeriesFilterField"]["enum"]
    statement = dto.library_filter(7)["statements"][0]
    assert dict(zip(values, names, strict=True))[statement["field"]] == "Libraries"
    comparisons = schemas["FilterComparison"]
    assert (
        dict(zip(comparisons["enum"], comparisons["x-enum-varnames"], strict=True))[
            statement["comparison"]
        ]
        == "Equal"
    )
