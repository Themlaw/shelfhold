"""M4 L53: the target of the page normalization is set by `SHELFHOLD_PAGE_TARGET_FORMAT`
(`jpeg` | `png` | `webp`) and `SHELFHOLD_PAGE_TARGET_QUALITY`; JPEG at 90 by default."""

import pytest

from shelfhold.config import ConfigError, load_settings


@pytest.mark.req("EF-EXP-046")
def test_default_target_is_jpeg_at_quality_90() -> None:
    values = load_settings({}).values
    assert (values.page_target_format, values.page_target_quality) == ("jpeg", 90)


@pytest.mark.req("EF-EXP-046")
def test_target_is_read_from_the_environment() -> None:
    values = load_settings(
        {"SHELFHOLD_PAGE_TARGET_FORMAT": "webp", "SHELFHOLD_PAGE_TARGET_QUALITY": "75"}
    ).values
    assert (values.page_target_format, values.page_target_quality) == ("webp", 75)


@pytest.mark.req("EF-EXP-046")
@pytest.mark.parametrize(
    "env",
    [
        {"SHELFHOLD_PAGE_TARGET_FORMAT": "avif"},  # never transcoded to AVIF or JXL
        {"SHELFHOLD_PAGE_TARGET_FORMAT": "jxl"},
        {"SHELFHOLD_PAGE_TARGET_QUALITY": "0"},
        {"SHELFHOLD_PAGE_TARGET_QUALITY": "101"},
    ],
)
def test_an_unusable_target_is_refused(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        load_settings(env)
