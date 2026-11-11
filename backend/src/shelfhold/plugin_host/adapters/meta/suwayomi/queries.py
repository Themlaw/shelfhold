"""The GraphQL documents the adapter sends: the whole allow-list (EF-SRC-033), always as JSON.

The mutations are the ones the spike S5 verified, in the shapes it verified (`spikes/s5`):
`enqueueChapterDownload`, `dequeueChapterDownload`, `updateManga(inLibrary | updateStrategy)`,
`createCategory`, `updateMangaCategories`, `deleteDownloadedChapter` and `fetchChapters` (the
refresh of the piloted mode, EF-SRC-025, M1b L21). Nothing here pages chapters or reaches an
extension: those operations do not exist in this module, and a test refuses any request outside
these root fields. `DELETE_DOWNLOADED_CHAPTER` is an erasure upstream: only the `release_claim`
capability sends it (L16 gives it its guards). `login` and `refreshToken` (EF-SRC-031,
`ui_login`) are the token mutations, sent by the host's JWT wrapper alone.
"""

import json

ABOUT = "query { aboutServer { name version buildType } }"
AUTH_MODE = "query { settings { authMode } }"
DOWNLOADS_PATH = "query { settings { downloadsPath } }"
# EF-SRC-026 (b): what the server is downloading now. The requeue waits until it is empty.
DOWNLOAD_QUEUE = "query { downloadStatus { state queue { chapter { id } } } }"
# EF-SRC-026 (d): the global update filters that leave out the series the user has not read
UPDATE_FILTERS = "query { settings { excludeUnreadChapters excludeNotStarted } }"
SOURCES = "query { sources { nodes { id displayName lang isNsfw } } }"
_MANGA_FIELDS = "id sourceId url title description author artist"
LIBRARY = f"query {{ mangas(condition: {{inLibrary: true}}) {{ nodes {{ {_MANGA_FIELDS} }} }} }}"
ALL_MANGAS = f"query {{ mangas {{ nodes {{ {_MANGA_FIELDS} }} }} }}"
CHAPTERS = (
    "query ($mangaId: Int!) { chapters(condition: {mangaId: $mangaId}) { nodes "
    "{ id url name chapterNumber sourceOrder scanlator uploadDate pageCount } } }"
)
CHAPTER = (
    "query ($id: Int!) { chapter(id: $id) { id mangaId url name chapterNumber isDownloaded } }"
)
ENQUEUE = (
    "mutation ($id: Int!) { enqueueChapterDownload(input: {id: $id}) { downloadStatus { state } } }"
)
# The piloted refresh of one series: Suwayomi asks the source and answers once it has learned
# the chapters (the call waits, so its return is the end of the refresh).
FETCH_CHAPTERS = "mutation ($id: Int!) { fetchChapters(input: {mangaId: $id}) { chapters { id } } }"
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

# EF-SRC-029: the library write of `follow`. The category is created excluded from the scheduled
# updates (`includeInUpdate: EXCLUDE`, the field `CATEGORIES` reads), its name is a literal so no
# schema type is named here.
MANGA_LIBRARY_STATE = (
    "query ($id: Int!) { manga(id: $id) { id inLibrary "
    "categories { nodes { id name includeInUpdate } } } }"
)
CREATE_SHELFHOLD_CATEGORY = (
    'mutation { createCategory(input: {name: "Shelfhold"}) { category { id name } } }'
)
# "Excluded from the automatic download" is `includeInDownload: EXCLUDE` (EF-SRC-009). Never
# `includeInUpdate`: it would stop Suwayomi refreshing the chapters of the followed series. Neither
# `updateCategory` nor `includeInDownload` is among what S5 measured and the repository pins no
# schema: the shape is the safest one, a plain create then this patch, to confirm against a real
# Suwayomi (nightly). A literal enum value, so that no schema type name is invented here.
EXCLUDE_CATEGORY_FROM_DOWNLOAD = (
    "mutation ($id: Int!) { updateCategory(input: {id: $id, patch: {includeInDownload: EXCLUDE}}) "
    "{ category { id includeInDownload } } }"
)
CATEGORY_DOWNLOAD = "query { categories { nodes { id name includeInDownload } } }"
# EF-SRC-033 `release_claim`: erases the downloaded file of a chapter on the Suwayomi side (S5)
DELETE_DOWNLOADED_CHAPTER = (
    "mutation ($id: Int!) { deleteDownloadedChapter(input: {id: $id}) { chapters { id } } }"
)


# EF-SRC-031 `ui_login`: the two token mutations, in the exact forms spike S5 ran against the
# pinned v2.4.2366 (`harness.login`, `test_ui_login_token_flow`). The secrets are string literals
# of the document (JSON escaping is GraphQL escaping), so a quote or a backslash stays inside.
# `refreshToken` is asked for its `accessToken` alone, as S5 did: whether the server also rotates
# the refresh token is not measured, and `JwtFetcher` keeps the one it holds in that case.
def login(username: str, password: str) -> str:
    return (
        f"mutation {{ login(input: {{username: {json.dumps(username)}, "
        f"password: {json.dumps(password)}}}) {{ accessToken refreshToken }} }}"
    )


def refresh_token(token: str) -> str:
    return (
        f"mutation {{ refreshToken(input: {{refreshToken: {json.dumps(token)}}}) "
        "{ accessToken } }"
    )


# D1: the roles of the logged-in account, read only when the server exposes them. v2.4.2366 does
# not (S5): the answer is then a GraphQL validation error and the account is accepted unchecked.
# This form is NOT confirmed against a real server (see docs/decisions/M1b.md, L20).
ACCOUNT_ROLES = "query { user { roles } }"

GRAPHQL_PATH = "/api/graphql"
DOWNLOAD_PATH = "/api/v1/chapter/{id}/download"
THUMBNAIL_PATH = "/api/v1/manga/{id}/thumbnail"
