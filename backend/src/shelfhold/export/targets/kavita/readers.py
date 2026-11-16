"""Who reads in the folders a rename plan touches (EF-EXP-037, EF-EXP-030).

A plan that renames a series makes Kavita scan again, and the readings of the series it loses go
with it. The progress of the members who hold a key here is carried (EF-EXP-017); the dedicated
account of the key reads nothing and is not counted. What is left are the accounts that could lose
progress: their number, an upper bound that says so (`exact` false), and their names (the admin
only sees them). With none left the count is exact: nobody. A Kavita that cannot be asked is never
"nobody".
"""

from collections.abc import Collection, Sequence

from shelfhold.export.application.rename_plan import ReaderCount
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.errors import TargetError
from shelfhold.infrastructure.kavita_names import kavita_name_key

__all__ = ["count_readers"]


async def count_readers(
    client: KavitaClient,
    library_id: int,
    remote_folders: Sequence[str],  # the folders asked about: Kavita cannot say who read where
    *,
    carried: Collection[str] = (),
) -> ReaderCount:
    """The accounts of Kavita that read in `remote_folders` of one library, or all that could.
    Apart from the count: the `carried` accounts (names, as the members declared them), whose
    progress a plan carries, and the dedicated account of the key, which no one reads with (the
    plan does not warn about an account that is never a reader, EF-EXP-037)."""
    kept = {kavita_name_key(name) for name in carried}
    try:
        service = await client.account_name()
        if service is not None:
            kept.add(kavita_name_key(service))
        users = await client.list_users()
    except TargetError:
        return ReaderCount(0, exact=False)  # Kavita could not be asked: not "nobody"
    left = [
        user for user in users
        if (user.is_admin or library_id in user.library_ids)
        and kavita_name_key(user.username) not in kept
    ]  # fmt: skip
    names = tuple(sorted(user.username for user in left))
    if not left:
        return ReaderCount(0, exact=True, accounts=names)
    return ReaderCount(len(left), exact=False, accounts=names)  # a count, not who read
