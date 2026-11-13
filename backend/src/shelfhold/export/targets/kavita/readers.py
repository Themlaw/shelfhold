"""Who reads in the folders a rename plan touches (EF-EXP-037, EF-EXP-030).

Before M4 the progress Kavita keeps for a reader is not carried over a rename, so the admin is told
how many readers would lose it. Kavita answers, with the key of the dedicated account, only for
that account (`pagesRead` of the key's own); the progress of the members is read with their own
tokens (EF-USR-012, M4). So the count is **exact** only where the key's account is the only one
with access to the library; with other accounts it is the number of accounts that could lose
progress, an upper bound that says so (`exact` false): the plan then asks for its confirmation like
any other (an unreadable count is never "nobody").
"""

from collections.abc import Sequence

from shelfhold.export.application.rename_plan import ReaderCount
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.errors import TargetError
from shelfhold.export.targets.kavita.series import SeriesResolver

__all__ = ["count_readers"]


async def count_readers(
    client: KavitaClient, library_id: int, remote_folders: Sequence[str]
) -> ReaderCount:
    """The accounts of Kavita that read in `remote_folders` of one library, or all that could."""
    try:
        users = await client.list_users()
        accounts = sum(1 for user in users if user.is_admin or library_id in user.library_ids)
        reading = await _key_account_reads(client, library_id, remote_folders)
    except TargetError:
        return ReaderCount(0, exact=False)  # Kavita could not be asked: not "nobody"
    if accounts <= 1:
        return ReaderCount(1 if reading else 0, exact=True)
    return ReaderCount(accounts, exact=False)  # the members' progress needs their own tokens (M4)


async def _key_account_reads(
    client: KavitaClient, library_id: int, remote_folders: Sequence[str]
) -> bool:
    resolver = SeriesResolver(client)
    for folder in remote_folders:
        found = await resolver.resolve(library_id=library_id, remote_folder=folder)
        if found.series is None:
            continue  # Kavita does not have it (yet): nobody has read it
        chapters = await client.series_chapters(found.series.id)
        if any(chapter.pages_read > 0 for chapter in chapters):
            return True
    return False
