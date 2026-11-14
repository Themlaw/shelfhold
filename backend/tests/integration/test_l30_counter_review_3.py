"""Third counter-review of L30 (M3, EF-EXP-031): a red proof for the erasing name.

`erase_host_area` renames `.shelfhold/` to `.shelfhold.erasing/` before emptying it. Kavita's
`ExcludePatterns` hold `**/.shelfhold/**`, a glob on a whole path segment: it does not match
`.shelfhold.erasing/`. While the folder is emptied (a long bin), and for good after a stop that
nobody reruns (giving up is refused past the point of no return), Kavita indexes what is left.
"""

import re
from collections.abc import Iterator
from typing import Any

import pytest

from shelfhold.export.application.detach import full_detach_phrase
from shelfhold.export.domain.physical_roots import ERASING_DIR
from shelfhold.export.targets.kavita.dto import HIDDEN_DIR_PATTERNS
from shelfhold.infrastructure.fsops.confined import ConfinedRoot
from tests.detach_env import ADMIN, DetachEnv, detach_env
from tests.rename_env import export_chapters

pytestmark = pytest.mark.req("EF-EXP-031")


class Killed(BaseException):
    """`kill -9`, seen from inside: nothing after it runs."""


def _glob(pattern: str) -> re.Pattern[str]:
    """Kavita's exclude glob, as far as these patterns go: `**/` any folders, `*` one segment."""
    out = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*")
    return re.compile("^" + out.replace(r"\*", "[^/]*") + "$")


def _excluded(rel: str, patterns: tuple[str, ...]) -> bool:
    return any(_glob(p).match(rel) for p in patterns)


@pytest.fixture
def d() -> Iterator[DetachEnv]:
    with detach_env() as made:
        export_chapters(made.env, "Dr Stone", ["1", "2"])
        made.revoker.fake = made.wired.fake
        yield made


def test_the_glob_reading_is_right() -> None:
    assert _excluded(".shelfhold/recycle/a/Ch.9.cbz", HIDDEN_DIR_PATTERNS)
    assert _excluded("Series/.shelfhold/x.cbz", HIDDEN_DIR_PATTERNS)


def test_a_reserved_folder_stopped_while_it_is_emptied_stays_excluded(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_entry = d.root / ".shelfhold" / "recycle" / "entry"
    bin_entry.mkdir(parents=True, exist_ok=True)
    (bin_entry / "Ch.9.cbz").write_bytes(b"a deleted book")
    real = ConfinedRoot.remove_tree

    def stop(self: ConfinedRoot, rel: str) -> Any:
        if rel == ERASING_DIR:  # the rename went through, the emptying is stopped
            raise Killed
        return real(self, rel)

    monkeypatch.setattr(ConfinedRoot, "remove_tree", stop)
    target = d.env.target_id
    with pytest.raises(Killed):
        d.detacher.apply(
            target, principal=ADMIN, typed=full_detach_phrase(target), client=d.wired.client,
            revoker=d.revoker, credential="the account password", discard_hidden_data=True,
            discard_tombstone_journal=True,
        )  # fmt: skip
    monkeypatch.undo()
    patterns = tuple(d.kavita_patterns())  # still Shelfhold's: the settings step was not reached
    shown = [
        str(p.relative_to(d.root))
        for p in d.root.rglob("*.cbz")
        if ".shelfhold" in str(p.relative_to(d.root))
        and not _excluded(str(p.relative_to(d.root)), patterns)
    ]
    assert shown == [], f"Kavita indexes these: {shown}"
