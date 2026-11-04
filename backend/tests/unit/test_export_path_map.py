"""EF-EXP-003 and EF-EXP-060: the path map between Shelfhold's mount and Kavita's."""

import pytest

from shelfhold.export.domain.path_map import PathEntry, PathMap, PathMapError, PathMode


@pytest.mark.req("EF-EXP-003")
def test_default_path_map_library_to_kavita_paths() -> None:
    path_map = PathMap.default()
    assert [(e.local, e.remote) for e in path_map.entries] == [
        ("/library/manga", "/manga"),
        ("/library/books", "/books"),
        ("/library/comics", "/comics"),
    ]
    assert all(e.mode is PathMode.RW for e in path_map.entries)
    # Shelfhold writes /library/manga/X/...; Kavita is asked to scan /manga/X.
    assert path_map.to_remote("/library/manga/X/Ch 1.cbz") == "/manga/X/Ch 1.cbz"
    assert path_map.to_remote("/library/manga/X") == "/manga/X"
    assert path_map.to_remote("/library/books/Y") == "/books/Y"
    assert path_map.to_local("/comics/Z/1.cbz") == "/library/comics/Z/1.cbz"
    assert path_map.to_local("/manga") == "/library/manga"


@pytest.mark.req("EF-EXP-003")
def test_same_path_option_maps_each_folder_to_itself() -> None:
    path_map = PathMap.same_path(["/library/manga", "/library/books"])
    assert path_map.to_remote("/library/manga/X") == "/library/manga/X"
    assert path_map.to_local("/library/books/Y") == "/library/books/Y"


@pytest.mark.req("EF-EXP-003")
def test_path_map_setting_round_trips() -> None:
    text = "/library/manga=/manga,/mnt/b=/books"
    path_map = PathMap.parse(text)
    assert path_map.to_remote("/mnt/b/Y") == "/books/Y"
    assert PathMap.parse(path_map.dump()) == path_map
    assert PathMap.parse("") == PathMap.default()


@pytest.mark.req("EF-EXP-003")
@pytest.mark.parametrize(
    "local",
    [
        "/library/manga2/X",  # a prefix of the name is not a prefix of the path
        "/library",
        "/elsewhere/X",
        "library/manga/X",  # relative
        "/library/manga/../books/Y",  # never normalised into another entry
        "/library/manga/\0x",
    ],
)
def test_path_outside_every_entry_is_refused(local: str) -> None:
    with pytest.raises(PathMapError):
        PathMap.default().to_remote(local)


@pytest.mark.req("EF-EXP-003")
@pytest.mark.parametrize(
    "entries",
    [
        [("/library/manga", "/manga"), ("/library/manga", "/other")],  # duplicate local
        [("/library/manga", "/manga"), ("/library/books", "/manga")],  # duplicate remote
        [("library/manga", "/manga")],
        [("/library/manga", "manga")],
        [("/library/manga/../x", "/manga")],
        [("/library/manga", "/manga/..")],
    ],
)
def test_ambiguous_or_unsafe_path_map_is_refused(entries: list[tuple[str, str]]) -> None:
    with pytest.raises(PathMapError):
        PathMap(tuple(PathEntry(local, remote) for local, remote in entries))


@pytest.mark.req("EF-EXP-060")
def test_adopt_ro_entry_is_flagged_and_has_no_remote_scan_path() -> None:
    path_map = PathMap(
        (
            PathEntry("/library/manga", "/manga"),
            PathEntry("/upstream/suwayomi", "/suwayomi", PathMode.ADOPT_RO),
        )
    )
    entry = path_map.entry_for("/upstream/suwayomi/Series/1.cbz")
    assert entry.mode is PathMode.ADOPT_RO
    assert [e.local for e in path_map.writable()] == ["/library/manga"]
