# ComicInfo XSD (pinned)

Downloaded once, outside CI, from the public repository `anansi-project/comicinfo` (the CI stays
offline). Both files are pinned by commit and by SHA-256.

| File | Upstream path | Last commit touching it | SHA-256 |
|---|---|---|---|
| `v2.0/ComicInfo.xsd` | `schema/v2.0/ComicInfo.xsd` | `75d00db57927bf6776717fc1060cb7f594186cb3` (2023-03-08) | `3d9109effff705014f5f6076d92b0ad8c8dad90d993f2592d744c10bad45311c` |
| `v2.1-draft/ComicInfo.xsd` | `drafts/v2.1/ComicInfo.xsd` | `0b6e0124adcde99eaf75d20cc924cc0b7eaf607c` (2023-03-15) | `c7a925b75a5297ec66b19898f59597077326a6b812d5ce77b3035fc179d75ed9` |

Both were read at repository commit `99e1453a163c777b4b5320a68732f6f133ac7918` (2026-06-23):

    gh api -H "Accept: application/vnd.github.raw" \
      "repos/anansi-project/comicinfo/contents/<upstream path>?ref=99e1453a163c777b4b5320a68732f6f133ac7918"

## What the schemas do not contain

Neither schema declares `LocalizedSeries` nor `SeriesSort`, the two non-standard elements that
Kavita reads (`Kavita.Models/Metadata/ComicInfo.cs`: `LocalizedSeries`, `SeriesSort`). The 2.1 draft
adds `Translator`, `Tags`, `StoryArcNumber` and `GTIN` to 2.0. The tests therefore validate a
Shelfhold ComicInfo twice: the document stripped of the two Kavita elements against the pinned 2.1
draft, and the whole document against that same draft with the two elements inserted after
`Series` (derived in memory by the test, never written back here).
