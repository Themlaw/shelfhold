"""The GraphQL documents the adapter sends: the whole allow-list (EF-SRC-033), always as JSON.

Nothing here refreshes, deletes, pages chapters or reaches an extension: those operations do not
exist in this module, and a test refuses any request outside these root fields.
"""

ABOUT = "query { aboutServer { name version buildType } }"
AUTH_MODE = "query { settings { authMode } }"
SOURCES = "query { sources { nodes { id displayName lang isNsfw } } }"
_MANGA_FIELDS = "id sourceId url title description author artist"
LIBRARY = f"query {{ mangas(condition: {{inLibrary: true}}) {{ nodes {{ {_MANGA_FIELDS} }} }} }}"
ALL_MANGAS = f"query {{ mangas {{ nodes {{ {_MANGA_FIELDS} }} }} }}"
CHAPTERS = (
    "query ($mangaId: Int!) { chapters(condition: {mangaId: $mangaId}) { nodes "
    "{ id url name chapterNumber sourceOrder scanlator uploadDate } } }"
)
CHAPTER = (
    "query ($id: Int!) { chapter(id: $id) { id mangaId url name chapterNumber isDownloaded } }"
)
ENQUEUE = (
    "mutation ($id: Int!) { enqueueChapterDownload(input: {id: $id}) { downloadStatus { state } } }"
)
DEQUEUE = (
    "mutation ($id: Int!) { dequeueChapterDownload(input: {id: $id}) { downloadStatus { state } } }"
)
SET_LIBRARY = (
    "mutation ($id: Int!, $inLibrary: Boolean!) { updateManga(input: "
    "{id: $id, patch: {inLibrary: $inLibrary}}) { manga { id inLibrary } } }"
)
# `detach --restore-upstream` (EF-EXP-056): the only writes besides downloads and the library flag
MANGA_SETTINGS = (
    "query ($id: Int!) { manga(id: $id) { id updateStrategy "
    "categories { nodes { id name includeInUpdate } } } }"
)
CATEGORIES = "query { categories { nodes { id name includeInUpdate } } }"
SET_MANGA_CATEGORIES = (
    "mutation ($id: Int!, $add: [Int!]!, $remove: [Int!]!) { updateMangaCategories(input: "
    "{id: $id, patch: {addToCategories: $add, removeFromCategories: $remove}}) "
    "{ manga { id } } }"
)
SET_UPDATE_STRATEGY = (
    "mutation ($id: Int!, $strategy: UpdateStrategy!) { updateManga(input: "
    "{id: $id, patch: {updateStrategy: $strategy}}) { manga { id } } }"
)

GRAPHQL_PATH = "/api/graphql"
DOWNLOAD_PATH = "/api/v1/chapter/{id}/download"
THUMBNAIL_PATH = "/api/v1/manga/{id}/thumbnail"
