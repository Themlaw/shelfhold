"""The deletion that `reconcile --adopt-after-restore --confirm-deletion` carries out (L35, L34).

The tombstones already exist (they are why the files are proposed): this only removes the files,
the way `purge --now` removes a copy: under the root's write session, with an intent recorded
first and closed after, through the one fsops primitive (`purge_unlink`), never a link, never a
directory, never a reserved file. Everything it cannot delete is reported, not hidden.
"""

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager

from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.restore import DeletionCandidate
from shelfhold.infrastructure.fsops import Fsops, FsopsError

__all__ = ["DeletionIncompleteError", "RestoreDeletion"]


class DeletionIncompleteError(FsopsError):
    """Some confirmed deletions could not be done: the paths, to delete by hand."""

    def __init__(self, paths: tuple[str, ...]) -> None:
        super().__init__(f"{len(paths)} file(s) could not be deleted")
        self.paths = paths


Sessions = Callable[..., AbstractContextManager[object]]


class RestoreDeletion:
    def __init__(
        self,
        fsops: Fsops,
        journal: IntentJournal,
        sessions: Sessions,
        root_paths: Mapping[str, str],
    ) -> None:
        self._root_paths = root_paths  # root id -> path
        self._fsops = fsops
        self._journal = journal
        self._sessions = sessions
        self.refused: list[tuple[str, str]] = []

    def purge(self, candidates: tuple[DeletionCandidate, ...]) -> None:
        by_root: dict[str, list[DeletionCandidate]] = {}
        for candidate in candidates:
            by_root.setdefault(candidate.root_id, []).append(candidate)
        for group in by_root.values():
            with self._sessions(self._root_paths[group[0].root_id]):
                for candidate in group:
                    self._erase(candidate)
        if self.refused:
            raise DeletionIncompleteError(tuple(sorted(p for p, _ in self.refused)))

    def _erase(self, candidate: DeletionCandidate) -> None:
        local = candidate.local_path
        try:
            seen = self._fsops.observe(local)
            if seen is None:
                return  # already gone
            if seen[0] != "file":
                self.refused.append((candidate.rel_path, "not_a_file"))
                return
            intent = self._journal.record(
                op="unlink", src=local, expected_sha256=_prefixed(self._fsops.sha256_of(local)),
                expected_file_id=seen[1],
            )  # fmt: skip
            self._journal.mark_applying(intent)
            try:
                self._fsops.purge_unlink(local)
            except FileNotFoundError:
                pass
            except FsopsError:
                self._journal.cancel(intent)
                raise
            self._journal.mark_done(intent)
        except FsopsError as error:
            self.refused.append((candidate.rel_path, type(error).__name__))


def _prefixed(digest: str) -> str:
    return digest if digest.startswith("sha256:") else "sha256:" + digest
