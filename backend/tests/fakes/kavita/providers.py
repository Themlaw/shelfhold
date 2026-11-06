"""Which metadata provider each library type takes, as Kavita 0.9.1.4 decides it.

Copied from upstream `KavitaPlusConfiguration.MetadataProvidersForLibraryTypes`
(Kavita.API/Services/Plus, tag v0.9.1.4): a create or update with another pair is answered 500
`invalid-metadata-provider`. Provider integers: the `MetadataProvider` enum (2 Hardcover,
3 Mangabaka, 4 ComicBookRoundup). Library types: the `LibraryType` enum.
"""

HARDCOVER, MANGABAKA, COMIC_BOOK_ROUNDUP = 2, 3, 4

PROVIDERS_FOR_LIBRARY_TYPE: dict[int, frozenset[int]] = {
    0: frozenset({MANGABAKA}),  # Manga
    1: frozenset({COMIC_BOOK_ROUNDUP, HARDCOVER}),  # Comic
    2: frozenset({HARDCOVER, MANGABAKA}),  # Book
    3: frozenset({MANGABAKA, COMIC_BOOK_ROUNDUP}),  # Image
    4: frozenset({MANGABAKA, HARDCOVER}),  # LightNovel
    5: frozenset({COMIC_BOOK_ROUNDUP, HARDCOVER}),  # ComicVine
}
