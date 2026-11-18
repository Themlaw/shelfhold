# Progress synchronisation with Kavita

The job `progress.sync` (class `background`, every ten minutes, parked by the maintenance mode like
any job) keeps each member's reading progress and the one Kavita holds for **their own account**
in step. It runs for a destination with `sync_progress` on, for every member whose own key is linked
and live (`destination_account`, L12). The switch is the binding's own and is off at the start: `shelfhold
target sync-progress <binding> --on`, `POST /api/v1/destinations/{id}/sync-progress` or the box of the
destinations screen (a reserved action, fresh admin session, journaled; M4 L46). Only a Kavita binding can
take it: a destination that keeps no reading position answers `destination.sync_unsupported`. The shape of the view it reads and writes through is
`docs/reference/reading-progress.md`; this page is the sync's contract.

## One pass, one member

1. **Who the key is.** `GET /api/Account` with the member's key; the name must be the one on file
   (same comparison as Kavita's). A key that now opens another account stops this profile
   (`account_mismatch`). A Kavita account that two profiles linked, on any binding of the same
   Kavita, is synchronised for neither (`account_shared`).
2. **Per chapter the member may see** (a chapter above a child's ceiling is not even asked about,
   so nothing of it is read, stored or written):
   * **Pull.** `GET /api/Reader/get-progress?chapterId=` with the member's key. A page that is news
     becomes a `server` position of that member, through `record_server_state` (the one door of an
     externally held state). A page equal to the last page Shelfhold **pushed** is the echo of its
     own write and is dropped; a page that has not moved since the last pass is not stored again.
   * **Push.** The page of the member's **latest `device` position since the last pass**, the
     latest by the **server's** clock (`server_ts`, then id; `client_ts` never counts). It is
     written with `POST /api/Reader/progress` only if it is **beyond** Kavita's own page, read
     again just before the write: a position is never moved back in Kavita.

**Cost of a pass.** The member's key is opened once and one connection serves the whole pass. Kavita
is first asked for the account's own series list (`POST /api/Series/all-v2`, one read per hundred
series): `pagesRead` and `latestReadDate` are the account's, and a series whose pair is the one
noted at the last pass, with no position of the member newer than its notes, is **not asked about at
all**: no call, no write. Only series that moved, or hold something new to say, are visited chapter
by chapter. Notes that changed are written together with the cursor, in one transaction: a pass
with nothing new writes the cursor and nothing else. A series list that cannot be read whole (more
than 5,000 series, an error) is not trusted: every chapter is asked, as before. Our own push changes
the pair, so the pass after a push looks at that series once more and is quiet from then on. A chapter
that `series-detail` lists twice (a numbered volume's chapters are in `chapters` and in
`volumes[].chapters`) is taken once, by its id.

A stop (pause, maintenance window) is read between two members and between two series: what is done
stays done and the rest waits for the next run.

Order inside a pass is fixed: all the reading comes before the writing of the same chapter, and a
member's pass ends before the next one starts.

## Pages

Kavita keeps one integer per account and chapter, `0` to the chapter's page count; `pageNum ==
pages` means read (`ReaderService.SaveReadingProgress`, `CapPageToChapter`, tag `v0.9.1.4`). Kavita
counts from 0.

* **Pulled**: `pages` stays `pages` (read, taken as it is, undoable by the member); any page short
  of it is recorded at most two pages short of the end, so a Kavita sitting on its last page index
  is never taken for a reading, however the pages of a file are counted.
* **Pushed**: never `pages` unless a traversal marked the Unit read (`reading_progress.state =
  'read'` for every Unit the file covers); otherwise `pages - 2` at most, the cap the pull applies.
  A look at the last page, in KOReader, pushed to Kavita and pulled back, stays a look: Kavita is
  given the page, not the mark, and the echo is not read. The cap is two pages, not one, because a
  KOReader that opens Kavita's page stream asks for the image after the saved page, and Kavita adds
  one to a page within two of the end: a look pushed at `pages - 1` would come back as « read ».
* **EPUB**: Kavita counts an EPUB in book documents, not in the pages the Units are bounded in. A
  file of that format is **neither pulled nor pushed** (its chapter is left out of the structure);
  the pass through the percentage is for a later lot (L51).
* A KOReader that reads through Kavita's OPDS-PSE is counted by Kavita itself, with an offset of
  one near the end (`OPDSController`, the `Koreader` user agent): Kavita then says « read », and
  that state is taken as Kavita's.

## Blocks of a series (kosync, M4 L50)

A position an e-reader reports on a block is stored on the file of its chapter (`locator.page` is the
page **in the chapter**), so the pass above relays it unchanged. Two things are added.

* **The page map is checked first.** Before any member's pass, Kavita's page count of each chapter
  (`series-detail`, the dedicated key) is compared with the `bundle_member` rows that hold the file
  (`reading.application.page_map`). Equal: the member is `verified`; different: `unverified`, with
  the count Kavita announced (`kavita_pages`); a member whose names Kavita might sort otherwise
  (`unverified` from the start) is never compared. A page that came through a block
  (`locator.bundle`) is pushed only if the chapter's member of that block is `verified`; else
  nothing is pushed for that chapter (`MemberOutcome.unverified`, log `progress_sync.chapter_not_relayed`,
  Health `relay.page_count_mismatch` / `relay.page_map_unverified`). The kosync read gives a page of
  Kavita back, in the frame of the block, only for a `verified` member. Without the keyring that
  compares (`page_map_keyring`) no member is verified and nothing of a block is relayed.
* **A chapter a read crossed is pushed as « read »** though it has no position of its own: when a
  traversal in the frame of the block marked every Unit of the file read since the chapter's note
  (`reading_position.read_units`), `pages` is pushed, never past what Kavita already holds. A look
  at a last page stays the cap of two pages short. The undo of a mark is not propagated.
* A position **transposed** from a chapter replaced since the block (`locator.transposed`: a share
  of the chapter, no page) counts in the merged progress but is never pushed.

## Clocks and races

Kavita's `lastModifiedUtc` is **its** clock. It is compared for equality with the last one seen
(« this changed ») and for nothing else, so a Kavita whose clock is days off decides nothing: what
is pulled is dated by Shelfhold's clock, and the order of two writes is the order of Shelfhold's
`server_ts`.

| Situation | Result |
|---|---|
| Kavita behind, Shelfhold ahead | pushed |
| Kavita ahead, Shelfhold behind | pulled, nothing pushed; `reading_progress` keeps the furthest |
| Two devices | the later one by the server's clock is pushed, if it is beyond Kavita |
| Both moved since the last pass | pull first; the push happens only if still beyond Kavita |
| Kavita unread (progress removed) | not propagated: positions already kept here stay |
| « Read » undone here | not propagated to Kavita, which stays « read »; it is not imposed again here while its `lastModifiedUtc` does not change, and a new reading there (the stamp changes) counts as news |

Kavita has no compare-and-swap: the page is read again just before the write, which narrows the
window and does not close it.

## Resilience

429, 5xx, a refused connection and a timeout are retried up to **three** tries, with a wait of
`Retry-After` when Kavita says one, else 1 s then 2 s, **never more than 30 s**. Past that the
member's pass ends `unavailable` and the remaining members wait for the next run: the job records
`paused`, never an error. Nothing is lost: the chapter's note is written only after the chapter is
done, so the next pass finds the same positions. A 403 on a chapter (the account may not open that
library) skips that chapter. A 401 says the key is refused: `token_refused`, with no fallback to the
service account; the other members go on.

Closed codes of a chapter that is not relayed (`page_map.CODES`): `page_count_mismatch`, `page_map_unverified`.

Closed codes of a member's pass: `ok`, `not_enabled`, `member_key_revoked`, `token_unreadable`,
`token_refused`, `account_mismatch`, `account_shared`, `unavailable`. `revoked_at` is read again
before each call, so a key revoked during a pass is not used for the next one.

## What is stored

`progress_sync_note` (migration 0078), per account and Kavita chapter: the page last pushed, the
state last seen (page and Kavita's stamp), when the chapter was last synchronised. It goes with its
account and is dropped with it. A note is made for one Kavita account and one key (`account_ref`:
the declared name and the key's fingerprint) and for one file (`carrier_id`): a member linked to
another Kavita account, or with another key, or a chapter id that now holds another file, starts
afresh (the row of an account survives a re-link, the note must not). `progress_sync_cursor` records, per account,
the start of the last complete pass. Neither is in `export-all`.

## Carried over a rename plan (M4 L16)

A rename, a reshelving or a binding migration inside one root makes Kavita scan again, and a scan
that no longer finds a series **deletes it with every reading of every account**. A rename plan
(`export/application/rename_plan.py`) therefore carries each member's progress, batch by batch (a
batch is a series folder), with the member's own key and in this order:

1. **pull** (before the batch's first move): what Kavita holds is read for every member whose key is
   held (`sync_member(mode="pull")`: reading only, the notes keep their dates, nothing is written in
   Kavita). A member whose pass does not end `ok` (key refused, revoked, Kavita down) **stops the
   batch before anything moved**: `progress_not_pulled` (409), the plan is `failed`, the admin fixes
   the key or unlinks it. There is no half-carried member.
2. **move**: the renames, as before (never a deletion followed by a recreation).
3. **scan**: one per batch, drained at its end.
4. **resolve_remote**: the series are found again by the folder they live in
   (`SeriesResolver.resolve`, by the folder: the cached id may be that of a series that no longer
   lives where the files now are); `export_series.kavita_series_id` follows what Kavita shows. A series Kavita does not show yet is waited for (6 tries, 5 s), then
   left to the periodic pass. `Resolution.renamed`: a series renamed in Kavita's interface is the same
   series and its name is read, never written.
5. **push** (`sync_member(mode="restore")`): the notes of the chapters that are gone are dropped and
   each member's **furthest proven** page is written to every chapter they may see
   (`reading_progress_proven`, never a position carried over from another Edition), whatever its
   source: Kavita's own state pulled at step 1 comes back as it was. Never behind what Kavita already
   holds; « read » (the page count) only if every Unit of the file is read in the proven view **and**
   a traversal of a recent contact marked one or Kavita itself said so; otherwise the page, capped
   as any push (`pages - 2`). Nothing is ever recorded as a new reading.

Every half is idempotent (the note of the pass remembers what was pushed, so its echo is never read
back as a reading), so a stop at any step is resumed by running the batch again; the resume of a
plan (`RenameBook.resume`) does it. The periodic pass also carries a chapter it has never seen under
an id Kavita has just made (a rescan that renumbered a series, with or without a plan), by the same
rule.

**Who is carried.** The members whose own live key the destination holds (`destination_account`),
on a destination with `sync_progress` on. The preview counts the readers of Kavita **without** their
accounts (`count_readers(carried=...)`), and `warnings[].members` names, to the admin only, the
members who declare a Kavita account (`user.kavita_username`) with no key here and whose account
reads in these folders: the confirmation of EF-EXP-037 is for them, and for the accounts nobody
declared. No warning, no confirmation when everybody is carried.

**What it does not do.** A move from one root to another is a copy, not a rename: it stays refused
(`cross_root`), noted « Later ». A series name already borne by another series of the library is
refused at the planning (`series_name_collision`, the names Kavita shows now: the series the user
renamed in Kavita is our own, never a collision). A chapter Kavita holds as several files, an EPUB
and a block page whose map is not verified are not carried (as in the periodic pass).

## Side effects in Kavita

Writing a progress makes Kavita record a reading session (its statistics) and, with Kavita+, send
the reading to the account's tracking service (`ScrobbleReadingUpdate`). What Shelfhold pushes for
a member therefore reaches that service too, **a child's account included**. There is no setting in
Shelfhold to cut the push for a child: the switch is `sync_progress` of the destination (off, nothing
is read or written for anyone), and the child's own key is theirs to link or not (`destination_account`).
A parent who does not want a child's reading sent on turns off scrobbling for that account in Kavita.

## Which keys, which routes

The structure (which Kavita chapter holds which exported file: ids, page counts and paths) is read
with the target's dedicated key through `series-detail`; **no progress is ever read or written with
it**. Progress goes through the closed list `MEMBER_ROUTES` (`docs/kavita-routes.md`) with the
member's key in `x-api-key` (`get-progress`, `progress`, `GET /api/Account`, and the series list
for its stamps), never in a URL, a log line or an error. The calls are those a Kavita
client or an OPDS-PSE client makes: a `ProgressDto` with `volumeId`, `chapterId`, `pageNum`,
`seriesId` and `libraryId`.

## Proof against Kavita 0.9.1

Read on GitHub at the tag `v0.9.1.4`: `Kavita.Models/DTOs/Progress/ProgressDto.cs`,
`Kavita.Server/Controllers/ReaderController.cs` (`get-progress`: a stub page 0 with no progress;
`progress`: 200 with no body), `Kavita.Services/Reading/ReaderService.cs` (`SaveReadingProgress`: an
unchanged page changes nothing, page 0 with no record creates none, the page is capped to the
count) and `Kavita.Server/Controllers/OPDSController.cs`. The cassettes in
`backend/tests/cassettes/kavita_progress/` are **synthetic**, fabricated from that source (they say
so), and replayed with no network; `tests/fakes/kavita` follows the same source.
