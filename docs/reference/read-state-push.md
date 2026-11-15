# Read state pushed back to Suwayomi

The job `read_state.push` (class `background`, every ten minutes, parked by the maintenance mode like
any job) tells Suwayomi which chapters have been read in Shelfhold. It exists so that a series read
here does not show up as unread in Suwayomi, and so that Suwayomi's own filters (the updates and
downloads that look at unread chapters) see the truth. What « read » means is the contract of
`docs/reference/reading-progress.md`; this page is the push's contract.

## Which profile speaks for Suwayomi

Suwayomi has **one user and no profiles**. Exactly one profile of Shelfhold speaks for it, the
**owner**:

* the setting `source.read_state_owner`, a user name (case does not matter);
* **empty (the default): the administrator** (the oldest administrator account).

A **child is never the owner and is never pushed**. A setting that names a child (a role `child`, or any
profile with an age ceiling) stops the whole push with the code `owner_is_child`; no other profile
takes over, and nothing is sent. A name that matches no profile stops it as well (`owner_unknown`).
A member who is not the owner is not pushed either: their reading stays in Shelfhold. Changing the
owner is a setting like any other (`config apply`, administrators only); there is no HTTP route, no
command and no key or device token that can start a push: the job is the only door.

## What is pushed

* A Unit counts as read **only as the `reading_progress` view says it**: a plausible traversal, or a
  state a server holds that is not the echo of Shelfhold's own push. **A look at the last page is
  never pushed.**
* **An undone mark propagates.** When the owner undoes a mark (`undo_read_mark`), the chapter is set
  to unread in Suwayomi, **but only if Shelfhold itself pushed it as read**: a chapter read in
  Suwayomi by its own user, and never pushed from here, is never set back to unread.
* **Nothing is read back.** Suwayomi's `isRead` never becomes a position: no echo to drop, no loop.
* Only chapters **Shelfhold holds a readable copy of** are pushed. What Suwayomi downloads or keeps
  does not change for the others (Suwayomi's automatic download and update filters look at unread
  chapters).

## Cost

`read_state_push_note` (migration 0080) is the cursor: per owner, binding and chapter, the state last
pushed (`read` or `unread`). A pass is the difference between the view and the notes, computed in the
database: **with nothing new, not one request leaves.** For a series with news: one listing of its
chapters (to skip what Suwayomi already says: it is noted, not written), then **one request per
batch of 100 chapters**, `read` and `unread` apart. The `isRead` patch alone is sent: never the page
(`lastPageRead` also moves `lastReadAt` and, with KOReader sync enabled in Suwayomi, its own sync),
never a bookmark. A chapter that Suwayomi no longer lists is left alone (a series with such a chapter
costs one listing per pass).

Setting a chapter read makes Suwayomi push its progress to its KOReader sync, if the admin enabled
it there: this is Suwayomi's own behaviour on `isRead`, not something Shelfhold asks for.

## Piloted mode and sleeping services

* The push **never asks Suwayomi to refresh** (no `fetchChapters`): it does not take part in the check
  of the updates, so there is no loop with the follow cycle, in `always_on` or in `piloted` mode.
* A service behind a WakeHook is **not woken for this**. If its wake session is not open, the pass is
  `asleep` and counted as a pause; the next pass pushes (the notes follow what was written). A service that
  cannot be reached is `unavailable`, also a pause, never a failure.

Closed codes of a pass: `ok`, `owner_is_child`, `owner_unknown`, `asleep`, `unavailable`, `refused`,
`stopped` (a pause or maintenance window read between two series).

## The API

Read in the source of Suwayomi-Server at the tag **v2.4.2366**, the minimum version Shelfhold supports
(`server/src/main/kotlin/suwayomi/tachidesk/graphql/mutations/ChapterMutation.kt`, `types/ChapterType.kt`):

```graphql
mutation { updateChapters(input: {ids: [5001, 5002], patch: {isRead: true}}) { chapters { id isRead } } }
```

`UpdateChaptersInput { clientMutationId, ids: [Int!]!, patch: UpdateChapterPatch! }` with
`UpdateChapterPatch { isBookmarked, isRead, lastPageRead }`. The ids are integer literals of the
document (no schema type is named, nothing but a number can reach it). The simulated server of the
tests (`tests/fakes/suwayomi`) is **synthetic**, written from that source, and accepts this one form
only.
