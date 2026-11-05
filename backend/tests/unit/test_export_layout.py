"""EF-EXP-014 (guilty test, last commit of the lot): Specials folder and Vol.X files."""

import pytest

from shelfhold.export.domain.naming import FrozenAttrs, export_path, volume_deduced
from tests.fakes.kavita.plugin_parse import SPECIAL_VOLUME, simulate_parse


@pytest.mark.req("EF-EXP-014")
def test_specials_and_volumes_layout() -> None:
    special = export_path(FrozenAttrs("Dr Stone", "special", "1"))
    assert special.rel_path == "Dr Stone/Specials/Dr Stone - Special 1.cbz"
    assert simulate_parse(special.filename).min_volume == SPECIAL_VOLUME
    annual = export_path(FrozenAttrs("Dr Stone", "annual", "2"))
    assert annual.directory == ("Dr Stone", "Specials")
    assert simulate_parse(annual.filename).min_volume == SPECIAL_VOLUME
    # Vol.X names closed volumes only (kind = volume), never a chapter of a running series
    volume = export_path(FrozenAttrs("Dr Stone", "main", "5", kind="volume"))
    assert volume.rel_path == "Dr Stone/Dr Stone - Vol.5.cbz"
    parsed = simulate_parse(volume.filename)
    assert volume_deduced(parsed) and parsed.min_volume == 5.0
    chapter = export_path(FrozenAttrs("Dr Stone", "main", "5"))
    assert chapter.rel_path == "Dr Stone/Dr Stone - Ch.5.cbz"
    assert not volume_deduced(simulate_parse(chapter.filename))
