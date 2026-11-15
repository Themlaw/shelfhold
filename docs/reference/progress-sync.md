# Progress synchronisation with Kavita

The job `progress.sync` (class `background`, every ten minutes, parked by the maintenance mode like
any job) keeps each member's reading progress and the one Kavita holds for **their own account**
in step. It runs for a destination with `sync_progress` on, for every member whose own key is linked
and live (`destination_account`, L12). The shape of the view it reads and writes through is
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
  'read'` for every Unit the file covers); otherwise the last page index at most. A look at the
  last page, in KOReader, pushed to Kavita and pulled back, stays a look: Kavita is given the page,
  not the mark, and the echo is not read.
* A KOReader that reads through Kavita's OPDS-PSE is counted by Kavita itself, with an offset of
  one near the end (`OPDSController`, the `Koreader` user agent): Kavita then says « read », and
  that state is taken as Kavita's.

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

Closed codes of a member's pass: `ok`, `not_enabled`, `member_key_revoked`, `token_unreadable`,
`token_refused`, `account_mismatch`, `account_shared`, `unavailable`. `revoked_at` is read again
before each call, so a key revoked during a pass is not used for the next one.

## What is stored

`progress_sync_note` (migration 0078), per account and Kavita chapter: the page last pushed, the
state last seen (page and Kavita's stamp), when the chapter was last synchronised. It goes with its
account and is dropped when an account is re-linked. `progress_sync_cursor` records, per account,
the start of the last complete pass. Neither is in `export-all`.

## Which keys, which routes

The structure (which Kavita chapter holds which exported file: ids, page counts and paths) is read
with the target's dedicated key through `series-detail`; **no progress is ever read or written with
it**. Progress goes through the closed list `MEMBER_ROUTES` (`docs/kavita-routes.md`) with the
member's key in `x-api-key`, never in a URL, a log line or an error. The calls are those a Kavita
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
