"""Who reads in the folders of a rename plan (M3 L29, EF-EXP-037): exact only where the key's own
account is the only one with access, an upper bound that says so otherwise, never "nobody" when
Kavita could not be asked."""

import asyncio
from dataclasses import dataclass, field

import pytest

from shelfhold.export.targets.kavita import dto
from shelfhold.export.targets.kavita.errors import TargetError
from shelfhold.export.targets.kavita.readers import count_readers

LIBRARY = 3


def _series(series_id: int, folder: str) -> dto.SeriesInfo:
    return dto.SeriesInfo(series_id, f"S{series_id}", LIBRARY, folder, folder, False)


def _user(name: str, *, admin: bool = False, libraries: frozenset[int] = frozenset()) -> dto.Member:
    return dto.Member(name, admin, 0, False, libraries)


@dataclass
class FakeClient:
    users: list[dto.Member] = field(default_factory=list)
    read_pages: dict[int, int] = field(default_factory=dict)
    series: list[dto.SeriesInfo] = field(default_factory=list)
    down: bool = False
    asked: list[int] = field(default_factory=list)

    async def list_users(self) -> list[dto.Member]:
        if self.down:
            raise TargetError("unreachable")
        return self.users

    async def series_of_library(self, library_id: int) -> list[dto.SeriesInfo]:
        return [s for s in self.series if s.library_id == library_id]

    async def series_by_ids(self, ids: list[int]) -> dict[int, dto.SeriesInfo]:
        return {}

    async def series_chapters(self, series_id: int) -> list[dto.ChapterFile]:
        self.asked.append(series_id)
        return [dto.ChapterFile("/m/a.cbz", 10, self.read_pages.get(series_id, 0))]


def _count(client: FakeClient, folders: list[str]) -> tuple[int, bool]:
    found = asyncio.run(count_readers(client, LIBRARY, folders))  # type: ignore[arg-type]
    return found.count, found.exact


@pytest.mark.req("EF-EXP-037")
def test_the_only_account_is_counted_exactly() -> None:
    client = FakeClient(
        users=[_user("key", admin=True)], series=[_series(1, "/m/A"), _series(2, "/m/B")],
        read_pages={1: 4},
    )  # fmt: skip
    assert _count(client, ["/m/A"]) == (1, True)
    assert _count(client, ["/m/B"]) == (0, True)
    assert _count(client, ["/m/unknown"]) == (0, True)  # Kavita does not hold it: nobody read it


@pytest.mark.req("EF-EXP-037")
def test_with_other_accounts_the_count_is_an_upper_bound_that_says_so() -> None:
    client = FakeClient(
        users=[_user("key", admin=True), _user("a", libraries=frozenset({LIBRARY})),
               _user("b", libraries=frozenset({LIBRARY})), _user("c", libraries=frozenset({9}))],
        series=[_series(1, "/m/A")],
    )  # fmt: skip
    assert _count(client, ["/m/A"]) == (3, False)  # `c` has no access to the library


@pytest.mark.req("EF-EXP-037")
def test_a_kavita_that_cannot_be_asked_is_not_nobody() -> None:
    assert _count(FakeClient(down=True), ["/m/A"]) == (0, False)
